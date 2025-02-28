import itertools
import os
import zlib

from typing import BinaryIO, Iterable, List, NamedTuple, Optional, Tuple, Union

import ctranslate2
import numpy as np
import tokenizers

from faster_whisper.audio import decode_audio
from faster_whisper.feature_extractor import FeatureExtractor
from faster_whisper.tokenizer import Tokenizer


class Word(NamedTuple):
    start: float
    end: float
    word: str
    probability: float


class Segment(NamedTuple):
    start: float
    end: float
    text: str
    words: Optional[List[Word]]


class AudioInfo(NamedTuple):
    language: str
    language_probability: float
    duration: float


class TranscriptionOptions(NamedTuple):
    beam_size: int
    best_of: int
    patience: float
    length_penalty: float
    log_prob_threshold: Optional[float]
    no_speech_threshold: Optional[float]
    compression_ratio_threshold: Optional[float]
    condition_on_previous_text: bool
    temperatures: List[float]
    initial_prompt: Optional[str]
    prefix: Optional[str]
    suppress_blank: bool
    suppress_tokens: Optional[List[int]]
    without_timestamps: bool
    max_initial_timestamp: float
    word_timestamps: bool
    prepend_punctuations: str
    append_punctuations: str


class WhisperModel:
    def __init__(
        self,
        model_path: str,
        device: str = "auto",
        device_index: Union[int, List[int]] = 0,
        compute_type: str = "default",
        cpu_threads: int = 0,
        num_workers: int = 1,
    ):
        """Initializes the Whisper model.

        Args:
          model_path: Path to the converted model.
          device: Device to use for computation ("cpu", "cuda", "auto").
          device_index: Device ID to use.
            The model can also be loaded on multiple GPUs by passing a list of IDs
            (e.g. [0, 1, 2, 3]). In that case, multiple transcriptions can run in parallel
            when transcribe() is called from multiple Python threads (see also num_workers).
          compute_type: Type to use for computation.
            See https://opennmt.net/CTranslate2/quantization.html.
          cpu_threads: Number of threads to use when running on CPU (4 by default).
            A non zero value overrides the OMP_NUM_THREADS environment variable.
          num_workers: When transcribe() is called from multiple Python threads,
            having multiple workers enables true parallelism when running the model
            (concurrent calls to self.model.generate() will run in parallel).
            This can improve the global throughput at the cost of increased memory usage.
        """
        self.model = ctranslate2.models.Whisper(
            model_path,
            device=device,
            device_index=device_index,
            compute_type=compute_type,
            intra_threads=cpu_threads,
            inter_threads=num_workers,
        )

        tokenizer_file = os.path.join(model_path, "tokenizer.json")
        if os.path.isfile(tokenizer_file):
            self.hf_tokenizer = tokenizers.Tokenizer.from_file(tokenizer_file)
        else:
            self.hf_tokenizer = tokenizers.Tokenizer.from_pretrained(
                "openai/whisper-tiny" + ("" if self.model.is_multilingual else ".en")
            )

        self.feature_extractor = FeatureExtractor()
        self.num_samples_per_token = self.feature_extractor.hop_length * 2
        self.frames_per_second = (
            self.feature_extractor.sampling_rate // self.feature_extractor.hop_length
        )
        self.tokens_per_second = (
            self.feature_extractor.sampling_rate // self.num_samples_per_token
        )
        self.input_stride = 2
        self.time_precision = 0.02
        self.max_length = 448

    def transcribe(
        self,
        audio: Union[str, BinaryIO, np.ndarray],
        language: Optional[str] = None,
        task: str = "transcribe",
        beam_size: int = 5,
        best_of: int = 5,
        patience: float = 1,
        length_penalty: float = 1,
        temperature: Union[float, List[float], Tuple[float, ...]] = [
            0.0,
            0.2,
            0.4,
            0.6,
            0.8,
            1.0,
        ],
        compression_ratio_threshold: Optional[float] = 2.4,
        log_prob_threshold: Optional[float] = -1.0,
        no_speech_threshold: Optional[float] = 0.6,
        condition_on_previous_text: bool = True,
        initial_prompt: Optional[str] = None,
        prefix: Optional[str] = None,
        suppress_blank: bool = True,
        suppress_tokens: Optional[List[int]] = [-1],
        without_timestamps: bool = False,
        max_initial_timestamp: float = 1.0,
        word_timestamps: bool = False,
        prepend_punctuations: str = "\"'“¿([{-",
        append_punctuations: str = "\"'.。,，!！?？:：”)]}、",
    ) -> Tuple[Iterable[Segment], AudioInfo]:
        """Transcribes an input file.

        Arguments:
          audio: Path to the input file (or a file-like object), or the audio waveform.
          language: The language spoken in the audio. It should be a language code such
            as "en" or "fr". If not set, the language will be detected in the first 30 seconds
            of audio.
          task: Task to execute (transcribe or translate).
          beam_size: Beam size to use for decoding.
          best_of: Number of candidates when sampling with non-zero temperature.
          patience: Beam search patience factor.
          length_penalty: Exponential length penalty constant.
          temperature: Temperature for sampling. It can be a tuple of temperatures,
            which will be successively used upon failures according to either
            `compression_ratio_threshold` or `logprob_threshold`.
          compression_ratio_threshold: If the gzip compression ratio is above this value,
            treat as failed.
          log_prob_threshold: If the average log probability over sampled tokens is
            below this value, treat as failed.
          no_speech_threshold: If the no_speech probability is higher than this value AND
            the average log probability over sampled tokens is below `logprob_threshold`,
            consider the segment as silent.
          condition_on_previous_text: If True, the previous output of the model is provided
            as a prompt for the next window; disabling may make the text inconsistent across
            windows, but the model becomes less prone to getting stuck in a failure loop,
            such as repetition looping or timestamps going out of sync.
          initial_prompt: Optional text to provide as a prompt for the first window.
          prefix: Optional text to provide as a prefix for the first window.
          suppress_blank: Suppress blank outputs at the beginning of the sampling.
          suppress_tokens: List of token IDs to suppress. -1 will suppress a default set
            of symbols as defined in the model config.json file.
          without_timestamps: Only sample text tokens.
          max_initial_timestamp: The initial timestamp cannot be later than this.
          word_timestamps: Extract word-level timestamps using the cross-attention pattern
            and dynamic time warping, and include the timestamps for each word in each segment.
          prepend_punctuations: If word_timestamps is True, merge these punctuation symbols
            with the next word
          append_punctuations: If word_timestamps is True, merge these punctuation symbols
            with the previous word

        Returns:
          A tuple with:

            - a generator over transcribed segments
            - an instance of AudioInfo
        """
        if not isinstance(audio, np.ndarray):
            audio = decode_audio(
                audio, sampling_rate=self.feature_extractor.sampling_rate
            )

        duration = audio.shape[0] / self.feature_extractor.sampling_rate
        features = self.feature_extractor(audio)

        if language is None:
            if not self.model.is_multilingual:
                language = "en"
                language_probability = 1
            else:
                segment = features[:, : self.feature_extractor.nb_max_frames]
                input = get_ctranslate2_storage(segment)
                results = self.model.detect_language(input)
                language_token, language_probability = results[0][0]
                language = language_token[2:-2]
        else:
            language_probability = 1

        tokenizer = Tokenizer(
            self.hf_tokenizer,
            self.model.is_multilingual,
            task=task,
            language=language,
        )

        options = TranscriptionOptions(
            beam_size=beam_size,
            best_of=best_of,
            patience=patience,
            length_penalty=length_penalty,
            log_prob_threshold=log_prob_threshold,
            no_speech_threshold=no_speech_threshold,
            compression_ratio_threshold=compression_ratio_threshold,
            condition_on_previous_text=condition_on_previous_text,
            temperatures=(
                temperature if isinstance(temperature, (list, tuple)) else [temperature]
            ),
            initial_prompt=initial_prompt,
            prefix=prefix,
            suppress_blank=suppress_blank,
            suppress_tokens=suppress_tokens,
            without_timestamps=without_timestamps,
            max_initial_timestamp=max_initial_timestamp,
            word_timestamps=word_timestamps,
            prepend_punctuations=prepend_punctuations,
            append_punctuations=append_punctuations,
        )

        segments = self.generate_segments(features, tokenizer, options)

        audio_info = AudioInfo(
            language=language,
            language_probability=language_probability,
            duration=duration,
        )

        return segments, audio_info

    def generate_segments(
        self,
        features: np.ndarray,
        tokenizer: Tokenizer,
        options: TranscriptionOptions,
    ) -> Iterable[Segment]:
        content_frames = features.shape[-1] - self.feature_extractor.nb_max_frames
        seek = 0
        all_tokens = []
        prompt_reset_since = 0

        if options.initial_prompt is not None:
            initial_prompt = " " + options.initial_prompt.strip()
            initial_prompt_tokens = tokenizer.encode(initial_prompt)
            all_tokens.extend(initial_prompt_tokens)

        while seek < content_frames:
            time_offset = seek * self.feature_extractor.time_per_frame
            segment = features[:, seek : seek + self.feature_extractor.nb_max_frames]
            segment_size = min(
                self.feature_extractor.nb_max_frames, content_frames - seek
            )
            segment_duration = segment_size * self.feature_extractor.time_per_frame

            previous_tokens = all_tokens[prompt_reset_since:]
            prompt = self.get_prompt(
                tokenizer,
                previous_tokens,
                without_timestamps=options.without_timestamps,
                prefix=options.prefix,
            )

            result, avg_log_prob, temperature = self.generate_with_fallback(
                segment, prompt, tokenizer, options
            )

            if options.no_speech_threshold is not None:
                # no voice activity check
                should_skip = result.no_speech_prob > options.no_speech_threshold

                if (
                    options.log_prob_threshold is not None
                    and avg_log_prob > options.log_prob_threshold
                ):
                    # don't skip if the logprob is high enough, despite the no_speech_prob
                    should_skip = False

                if should_skip:
                    # fast-forward to the next segment boundary
                    seek += segment_size
                    continue

            tokens = result.sequences_ids[0]

            previous_seek = seek
            current_segments = []

            single_timestamp_ending = (
                len(tokens) >= 2
                and tokens[-2] < tokenizer.timestamp_begin
                and tokens[-1] >= tokenizer.timestamp_begin
            )

            consecutive_timestamps = [
                i
                for i in range(len(tokens))
                if i > 0
                and tokens[i] >= tokenizer.timestamp_begin
                and tokens[i - 1] >= tokenizer.timestamp_begin
            ]

            if len(consecutive_timestamps) > 0:
                slices = list(consecutive_timestamps)
                if single_timestamp_ending:
                    slices.append(len(tokens))

                last_slice = 0
                for current_slice in slices:
                    sliced_tokens = tokens[last_slice:current_slice]
                    start_timestamp_position = (
                        sliced_tokens[0] - tokenizer.timestamp_begin
                    )
                    end_timestamp_position = (
                        sliced_tokens[-1] - tokenizer.timestamp_begin
                    )
                    start_time = (
                        time_offset + start_timestamp_position * self.time_precision
                    )
                    end_time = (
                        time_offset + end_timestamp_position * self.time_precision
                    )

                    current_segments.append(
                        dict(
                            seek=seek,
                            start=start_time,
                            end=end_time,
                            tokens=sliced_tokens,
                        )
                    )
                    last_slice = current_slice

                if single_timestamp_ending:
                    # single timestamp at the end means no speech after the last timestamp.
                    seek += segment_size
                else:
                    # otherwise, ignore the unfinished segment and seek to the last timestamp
                    last_timestamp_position = (
                        tokens[last_slice - 1] - tokenizer.timestamp_begin
                    )
                    seek += last_timestamp_position * self.input_stride

            else:
                duration = segment_duration
                timestamps = [
                    token for token in tokens if token >= tokenizer.timestamp_begin
                ]
                if len(timestamps) > 0 and timestamps[-1] != tokenizer.timestamp_begin:
                    last_timestamp_position = timestamps[-1] - tokenizer.timestamp_begin
                    duration = last_timestamp_position * self.time_precision

                current_segments.append(
                    dict(
                        seek=seek,
                        start=time_offset,
                        end=time_offset + duration,
                        tokens=tokens,
                    )
                )

                seek += segment_size

            if not options.condition_on_previous_text or temperature > 0.5:
                prompt_reset_since = len(all_tokens)

            if options.word_timestamps:
                self.add_word_timestamps(
                    current_segments,
                    tokenizer,
                    segment,
                    segment_size,
                    options.prepend_punctuations,
                    options.append_punctuations,
                )

                word_end_timestamps = [
                    w["end"] for s in current_segments for w in s["words"]
                ]

                if not single_timestamp_ending and len(word_end_timestamps) > 0:
                    seek_shift = round(
                        (word_end_timestamps[-1] - time_offset) * self.frames_per_second
                    )

                    if seek_shift > 0:
                        seek = previous_seek + seek_shift

            for segment in current_segments:
                tokens = segment["tokens"]
                text = tokenizer.decode(tokens)

                if segment["start"] == segment["end"] or not text.strip():
                    continue

                all_tokens.extend(tokens)

                yield Segment(
                    start=segment["start"],
                    end=segment["end"],
                    text=text,
                    words=(
                        [Word(**word) for word in segment["words"]]
                        if options.word_timestamps
                        else None
                    ),
                )

    def generate_with_fallback(
        self,
        segment: np.ndarray,
        prompt: List[int],
        tokenizer: Tokenizer,
        options: TranscriptionOptions,
    ) -> Tuple[ctranslate2.models.WhisperGenerationResult, float, float]:
        features = get_ctranslate2_storage(segment)
        result = None
        avg_log_prob = None
        final_temperature = None

        max_initial_timestamp_index = int(
            round(options.max_initial_timestamp / self.time_precision)
        )

        for temperature in options.temperatures:
            if temperature > 0:
                kwargs = {
                    "beam_size": 1,
                    "num_hypotheses": options.best_of,
                    "sampling_topk": 0,
                    "sampling_temperature": temperature,
                }
            else:
                kwargs = {
                    "beam_size": options.beam_size,
                    "patience": options.patience,
                }

            final_temperature = temperature
            result = self.model.generate(
                features,
                [prompt],
                length_penalty=options.length_penalty,
                max_length=self.max_length,
                return_scores=True,
                return_no_speech_prob=True,
                suppress_blank=options.suppress_blank,
                suppress_tokens=options.suppress_tokens,
                max_initial_timestamp_index=max_initial_timestamp_index,
                **kwargs,
            )[0]

            tokens = result.sequences_ids[0]

            # Recover the average log prob from the returned score.
            seq_len = len(tokens)
            cum_log_prob = result.scores[0] * (seq_len**options.length_penalty)
            avg_log_prob = cum_log_prob / (seq_len + 1)

            text = tokenizer.decode(tokens).strip()
            compression_ratio = get_compression_ratio(text)

            needs_fallback = False

            if (
                options.compression_ratio_threshold is not None
                and compression_ratio > options.compression_ratio_threshold
            ):
                needs_fallback = True  # too repetitive

            if (
                options.log_prob_threshold is not None
                and avg_log_prob < options.log_prob_threshold
            ):
                needs_fallback = True  # average log probability is too low

            if not needs_fallback:
                break

        return result, avg_log_prob, final_temperature

    def get_prompt(
        self,
        tokenizer: Tokenizer,
        previous_tokens: List[int],
        without_timestamps: bool = False,
        prefix: Optional[str] = None,
    ) -> List[int]:
        prompt = []

        if previous_tokens:
            prompt.append(tokenizer.sot_prev)
            prompt.extend(previous_tokens[-(self.max_length // 2 - 1) :])

        prompt.extend(tokenizer.sot_sequence)

        if without_timestamps:
            prompt.append(tokenizer.no_timestamps)

        if prefix:
            prefix_tokens = tokenizer.encode(" " + prefix.strip())
            if len(prefix_tokens) >= self.max_length // 2:
                prefix_tokens = prefix_tokens[: self.max_length // 2 - 1]
            prompt.extend(prefix_tokens)

        return prompt

    def add_word_timestamps(
        self,
        segments: List[dict],
        tokenizer: Tokenizer,
        mel: np.ndarray,
        num_frames: int,
        prepend_punctuations: str,
        append_punctuations: str,
    ):
        if len(segments) == 0:
            return

        text_tokens_per_segment = [
            [token for token in segment["tokens"] if token < tokenizer.eot]
            for segment in segments
        ]

        text_tokens = list(itertools.chain.from_iterable(text_tokens_per_segment))
        alignment = self.find_alignment(tokenizer, text_tokens, mel, num_frames)
        merge_punctuations(alignment, prepend_punctuations, append_punctuations)

        time_offset = (
            segments[0]["seek"]
            * self.feature_extractor.hop_length
            / self.feature_extractor.sampling_rate
        )

        word_index = 0

        for segment, text_tokens in zip(segments, text_tokens_per_segment):
            saved_tokens = 0
            words = []

            while word_index < len(alignment) and saved_tokens < len(text_tokens):
                timing = alignment[word_index]

                if timing["word"]:
                    words.append(
                        dict(
                            word=timing["word"],
                            start=round(time_offset + timing["start"], 2),
                            end=round(time_offset + timing["end"], 2),
                            probability=timing["probability"],
                        )
                    )

                saved_tokens += len(timing["tokens"])
                word_index += 1

            if len(words) > 0:
                # adjust the segment-level timestamps based on the word-level timestamps
                segment["start"] = words[0]["start"]
                segment["end"] = words[-1]["end"]

            segment["words"] = words

    def find_alignment(
        self,
        tokenizer: Tokenizer,
        text_tokens: List[int],
        mel: np.ndarray,
        num_frames: int,
        median_filter_width: int = 7,
    ) -> List[dict]:
        if len(text_tokens) == 0:
            return []

        result = self.model.align(
            get_ctranslate2_storage(mel),
            tokenizer.sot_sequence,
            [text_tokens],
            num_frames,
            median_filter_width=median_filter_width,
        )[0]

        text_token_probs = result.text_token_probs

        alignments = result.alignments
        text_indices = np.array([pair[0] for pair in alignments])
        time_indices = np.array([pair[1] for pair in alignments])

        words, word_tokens = tokenizer.split_to_word_tokens(
            text_tokens + [tokenizer.eot]
        )
        word_boundaries = np.pad(np.cumsum([len(t) for t in word_tokens[:-1]]), (1, 0))

        jumps = np.pad(np.diff(text_indices), (1, 0), constant_values=1).astype(bool)
        jump_times = time_indices[jumps] / self.tokens_per_second
        start_times = jump_times[word_boundaries[:-1]]
        end_times = jump_times[word_boundaries[1:]]
        word_probabilities = [
            np.mean(text_token_probs[i:j])
            for i, j in zip(word_boundaries[:-1], word_boundaries[1:])
        ]

        # hack: ensure the first and second word is not longer than twice the median word duration.
        # a better segmentation algorithm based on VAD should be able to replace this.
        word_durations = end_times - start_times
        word_durations = word_durations[word_durations.nonzero()]
        if len(word_durations) > 0:
            median_duration = np.median(word_durations)
            max_duration = median_duration * 2
            if len(word_durations) >= 2 and word_durations[1] > max_duration:
                boundary = max(end_times[2] / 2, end_times[2] - max_duration)
                end_times[0] = start_times[1] = boundary
            if (
                len(word_durations) >= 1
                and end_times[0] - start_times[0] > max_duration
            ):
                start_times[0] = max(0, end_times[0] - max_duration)

        return [
            dict(
                word=word, tokens=tokens, start=start, end=end, probability=probability
            )
            for word, tokens, start, end, probability in zip(
                words, word_tokens, start_times, end_times, word_probabilities
            )
        ]


def get_ctranslate2_storage(segment: np.ndarray) -> ctranslate2.StorageView:
    segment = np.ascontiguousarray(segment)
    segment = np.expand_dims(segment, 0)
    segment = ctranslate2.StorageView.from_array(segment)
    return segment


def get_compression_ratio(text: str) -> float:
    text_bytes = text.encode("utf-8")
    return len(text_bytes) / len(zlib.compress(text_bytes))


def merge_punctuations(alignment: List[dict], prepended: str, appended: str):
    # merge prepended punctuations
    i = len(alignment) - 2
    j = len(alignment) - 1
    while i >= 0:
        previous = alignment[i]
        following = alignment[j]
        if previous["word"].startswith(" ") and previous["word"].strip() in prepended:
            # prepend it to the following word
            following["word"] = previous["word"] + following["word"]
            following["tokens"] = previous["tokens"] + following["tokens"]
            previous["word"] = ""
            previous["tokens"] = []
        else:
            j = i
        i -= 1

    # merge appended punctuations
    i = 0
    j = 1
    while j < len(alignment):
        previous = alignment[i]
        following = alignment[j]
        if not previous["word"].endswith(" ") and following["word"] in appended:
            # append it to the previous word
            previous["word"] = previous["word"] + following["word"]
            previous["tokens"] = previous["tokens"] + following["tokens"]
            following["word"] = ""
            following["tokens"] = []
        else:
            i = j
        j += 1
