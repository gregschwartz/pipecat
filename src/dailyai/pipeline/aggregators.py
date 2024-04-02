import asyncio
import re

from dailyai.pipeline.frame_processor import FrameProcessor

from dailyai.pipeline.frames import (
    EndFrame,
    EndPipeFrame,
    Frame,
    LLMMessagesFrame,
    LLMResponseEndFrame,
    LLMResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from dailyai.pipeline.pipeline import Pipeline
from dailyai.services.ai_services import AIService

from typing import AsyncGenerator, Coroutine, List


class ResponseAggregator(FrameProcessor):

    def __init__(
        self,
        *,
        messages: list[dict] | None,
        role: str,
        start_frame,
        end_frame,
        accumulator_frame,
        pass_through=True,
    ):
        self.aggregation = ""
        self.aggregating = False
        self.messages = messages
        self._role = role
        self._start_frame = start_frame
        self._end_frame = end_frame
        self._accumulator_frame = accumulator_frame
        self._pass_through = pass_through

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        if not self.messages:
            return

        if isinstance(frame, self._start_frame):
            self.aggregating = True
        elif isinstance(frame, self._end_frame):
            self.aggregating = False
            # Sometimes VAD triggers quickly on and off. If we don't get any transcription,
            # it creates empty LLM message queue frames
            if len(self.aggregation) > 0:
                self.messages.append(
                    {"role": self._role, "content": self.aggregation})
                self.aggregation = ""
                yield self._end_frame()
                yield LLMMessagesFrame(self.messages)
        elif isinstance(frame, self._accumulator_frame) and self.aggregating:
            self.aggregation += f" {frame.text}"
            if self._pass_through:
                yield frame
        else:
            yield frame


class LLMResponseAggregator(ResponseAggregator):
    def __init__(self, messages: list[dict]):
        super().__init__(
            messages=messages,
            role="assistant",
            start_frame=LLMResponseStartFrame,
            end_frame=LLMResponseEndFrame,
            accumulator_frame=TextFrame,
        )


class UserResponseAggregator(ResponseAggregator):
    def __init__(self, messages: list[dict]):
        super().__init__(
            messages=messages,
            role="user",
            start_frame=UserStartedSpeakingFrame,
            end_frame=UserStoppedSpeakingFrame,
            accumulator_frame=TranscriptionFrame,
            pass_through=False,
        )


class LLMContextAggregator(AIService):
    def __init__(
        self,
        messages: list[dict],
        role: str,
        bot_participant_id=None,
        complete_sentences=True,
        pass_through=True,
    ):
        super().__init__()
        self.messages = messages
        self.bot_participant_id = bot_participant_id
        self.role = role
        self.sentence = ""
        self.complete_sentences = complete_sentences
        self.pass_through = pass_through

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        # We don't do anything with non-text frames, pass it along to next in
        # the pipeline.
        if not isinstance(frame, TextFrame):
            yield frame
            return

        # Ignore transcription frames from the bot
        if isinstance(frame, TranscriptionFrame):
            if frame.participantId == self.bot_participant_id:
                return

        # The common case for "pass through" is receiving frames from the LLM that we'll
        # use to update the "assistant" LLM messages, but also passing the text frames
        # along to a TTS service to be spoken to the user.
        if self.pass_through:
            yield frame

        # TODO: split up transcription by participant
        if self.complete_sentences:
            # type: ignore -- the linter thinks this isn't a TextFrame, even
            # though we check it above
            self.sentence += frame.text
            if self.sentence.endswith((".", "?", "!")):
                self.messages.append(
                    {"role": self.role, "content": self.sentence})
                self.sentence = ""
                yield LLMMessagesFrame(self.messages)
        else:
            # type: ignore -- the linter thinks this isn't a TextFrame, even
            # though we check it above
            self.messages.append({"role": self.role, "content": frame.text})
            yield LLMMessagesFrame(self.messages)


class LLMUserContextAggregator(LLMContextAggregator):
    def __init__(
            self,
            messages: list[dict],
            bot_participant_id=None,
            complete_sentences=True):
        super().__init__(
            messages,
            "user",
            bot_participant_id,
            complete_sentences,
            pass_through=False)


class LLMAssistantContextAggregator(LLMContextAggregator):
    def __init__(
            self,
            messages: list[dict],
            bot_participant_id=None,
            complete_sentences=True):
        super().__init__(
            messages,
            "assistant",
            bot_participant_id,
            complete_sentences,
            pass_through=True,
        )


class SentenceAggregator(FrameProcessor):
    """This frame processor aggregates text frames into complete sentences. It separates
    frames by participant if the participant_id field is defined in the TextFrame.

    Frame input/output:
        TextFrame("Hello,") -> None
        TextFrame(" world.") -> TextFrame("Hello world.")

    Doctest:
    >>> async def print_frames(aggregator, frame):
    ...     async for frame in aggregator.process_frame(frame):
    ...         print(frame.text)

    >>> aggregator = SentenceAggregator()
    >>> asyncio.run(print_frames(aggregator, TextFrame("Hello,", participantId="abcd")))
    >>> asyncio.run(print_frames(aggregator, TextFrame(" world.", participantId="cdef")))
     world.
    >>> asyncio.run(print_frames(aggregator, TextFrame(" everyone.", participantId="abcd")))
    Hello, everyone.
    """

    def __init__(self):
        self.aggregations = {}

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        if isinstance(frame, TextFrame):
            pax_id = "none"
            if frame.participantId:
                pax_id = frame.participantId
                if pax_id not in self.aggregations:
                    self.aggregations[pax_id] = ""
            m = re.search("(.*[?.!])(.*)", frame.text)
            if m:
                yield TextFrame(self.aggregations[pax_id] + m.group(1), participantId=pax_id)
                self.aggregations[pax_id] = m.group(2)
            else:
                self.aggregations[pax_id] += frame.text
        elif isinstance(frame, EndFrame):
            for key in self.aggregations:
                if self.aggregations[key]:
                    yield TextFrame(self.aggregation, participantId=key)
        else:
            yield frame


class LLMFullResponseAggregator(FrameProcessor):
    """This class aggregates Text frames until it receives a
    LLMResponseEndFrame, then emits the concatenated text as
    a single text frame.

    given the following frames:

        TextFrame("Hello,")
        TextFrame(" world.")
        TextFrame(" I am")
        TextFrame(" an LLM.")
        LLMResponseEndFrame()]

    this processor will yield nothing for the first 4 frames, then

        TextFrame("Hello, world. I am an LLM.")
        LLMResponseEndFrame()

    when passed the last frame.

    >>> async def print_frames(aggregator, frame):
    ...     async for frame in aggregator.process_frame(frame):
    ...         if isinstance(frame, TextFrame):
    ...             print(frame.text)
    ...         else:
    ...             print(frame.__class__.__name__)

    >>> aggregator = LLMFullResponseAggregator()
    >>> asyncio.run(print_frames(aggregator, TextFrame("Hello,")))
    >>> asyncio.run(print_frames(aggregator, TextFrame(" world.")))
    >>> asyncio.run(print_frames(aggregator, TextFrame(" I am")))
    >>> asyncio.run(print_frames(aggregator, TextFrame(" an LLM.")))
    >>> asyncio.run(print_frames(aggregator, LLMResponseEndFrame()))
    Hello, world. I am an LLM.
    LLMResponseEndFrame
    """

    def __init__(self):
        self.aggregations = {}

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        if isinstance(frame, TextFrame):
            pax_id = "none"
            if frame.participantId:
                pax_id = frame.participantId
            if pax_id not in self.aggregations:
                self.aggregations[pax_id] = ""
            self.aggregations[pax_id] += frame.text
        elif isinstance(frame, LLMResponseEndFrame):
            pax_id = "none"
            if frame.participantId:
                pax_id = frame.participantId
            if self.aggregations[pax_id]:
                yield TextFrame(self.aggregations[pax_id], participantId=pax_id)
            yield frame
            self.aggregations[pax_id] = ""
        else:
            yield frame


class StatelessTextTransformer(FrameProcessor):
    """This processor calls the given function on any text in a text frame.

    >>> async def print_frames(aggregator, frame):
    ...     async for frame in aggregator.process_frame(frame):
    ...         print(frame.text)

    >>> aggregator = StatelessTextTransformer(lambda x: x.upper())
    >>> asyncio.run(print_frames(aggregator, TextFrame("Hello")))
    HELLO
    """

    def __init__(self, transform_fn):
        self.transform_fn = transform_fn

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        if isinstance(frame, TextFrame):
            result = self.transform_fn(frame.text)
            if isinstance(result, Coroutine):
                result = await result

            yield TextFrame(result)
        else:
            yield frame


class ParallelPipeline(FrameProcessor):
    """Run multiple pipelines in parallel.

    This class takes frames from its source queue and sends them to each
    sub-pipeline. Each sub-pipeline emits its frames into this class's
    sink queue. No guarantees are made about the ordering of frames in
    the sink queue (that is, no sub-pipeline has higher priority than
    any other, frames are put on the sink in the order they're emitted
    by the sub-pipelines).

    After each frame is taken from this class's source queue and placed
    in each sub-pipeline's source queue, an EndPipeFrame is put on each
    sub-pipeline's source queue. This indicates to the sub-pipe runner
    that it should exit.

    Since frame handlers pass through unhandled frames by convention, this
    class de-dupes frames in its sink before yielding them.
    """

    def __init__(self, pipeline_definitions: List[List[FrameProcessor]]):
        self.sources = [asyncio.Queue() for _ in pipeline_definitions]
        self.sink: asyncio.Queue[Frame] = asyncio.Queue()
        self.pipelines: list[Pipeline] = [
            Pipeline(
                pipeline_definition,
                source,
                self.sink,
            )
            for source, pipeline_definition in zip(self.sources, pipeline_definitions)
        ]

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        for source in self.sources:
            await source.put(frame)
            await source.put(EndPipeFrame())

        await asyncio.gather(*[pipeline.run_pipeline() for pipeline in self.pipelines])

        seen_ids = set()
        while not self.sink.empty():
            frame = await self.sink.get()

            # de-dup frames. Because the convention is to yield a frame that isn't processed,
            # each pipeline will likely yield the same frame, so we will end up with _n_ copies
            # of unprocessed frames where _n_ is the number of parallel pipes that don't
            # process that frame.
            if id(frame) in seen_ids:
                continue
            seen_ids.add(id(frame))

            # Skip passing along EndPipeFrame, because we use them
            # for our own flow control.
            if not isinstance(frame, EndPipeFrame):
                yield frame


class GatedAggregator(FrameProcessor):
    """Accumulate frames, with custom functions to start and stop accumulation.
    Yields gate-opening frame before any accumulated frames, then ensuing frames
    until and not including the gate-closed frame.

    >>> from dailyai.pipeline.frames import ImageFrame

    >>> async def print_frames(aggregator, frame):
    ...     async for frame in aggregator.process_frame(frame):
    ...         if isinstance(frame, TextFrame):
    ...             print(frame.text)
    ...         else:
    ...             print(frame.__class__.__name__)

    >>> aggregator = GatedAggregator(
    ...     gate_close_fn=lambda x: isinstance(x, LLMResponseStartFrame),
    ...     gate_open_fn=lambda x: isinstance(x, ImageFrame),
    ...     start_open=False)
    >>> asyncio.run(print_frames(aggregator, TextFrame("Hello")))
    >>> asyncio.run(print_frames(aggregator, TextFrame("Hello again.")))
    >>> asyncio.run(print_frames(aggregator, ImageFrame(url='', image=bytes([]))))
    ImageFrame
    Hello
    Hello again.
    >>> asyncio.run(print_frames(aggregator, TextFrame("Goodbye.")))
    Goodbye.
    """

    def __init__(self, gate_open_fn, gate_close_fn, start_open):
        self.gate_open_fn = gate_open_fn
        self.gate_close_fn = gate_close_fn
        self.gate_open = start_open
        self.accumulator: List[Frame] = []

    async def process_frame(self, frame: Frame) -> AsyncGenerator[Frame, None]:
        if self.gate_open:
            if self.gate_close_fn(frame):
                self.gate_open = False
        else:
            if self.gate_open_fn(frame):
                self.gate_open = True

        if self.gate_open:
            yield frame
            if self.accumulator:
                for frame in self.accumulator:
                    yield frame
            self.accumulator = []
        else:
            self.accumulator.append(frame)


if __name__ == "__main__":
    import doctest
    doctest.testmod()
