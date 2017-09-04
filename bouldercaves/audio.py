"""
Various audio output options. Here the specific audio library code is located.
Supported audio output libraries:
- pyaudio
- sounddevice (both thread+blocking stream, and nonblocking callback stream variants)
- winsound

Written by Irmen de Jong (irmen@razorvine.net) - License: MIT open-source.
"""

import threading
import queue
import os
import time
import wave
import pkgutil
import io
import subprocess
import tempfile
import audioop


__all__ = ["PyAudio", "Sounddevice", "SounddeviceThread", "Winsound", "best_api", "Output"]


# stubs for optional audio library modules:
sounddevice = None
pyaudio = None
winsound = None

norm_samplerate = 44100
norm_samplewidth = 2
norm_channels = 2


def best_api(dummy_enabled=False):
    try:
        return Sounddevice()
    except ImportError:
        try:
            return SounddeviceThread()
        except ImportError:
            try:
                return PyAudio()
            except ImportError:
                try:
                    return Winsound()
                except ImportError:
                    if dummy_enabled:
                        return DummyAudio()
                    raise Exception("no suitable audio output api available") from None


class Sample:
    """A sample of raw PCM audio data. Uncompresses .ogg to PCM if needed."""
    def __init__(self, name, filename=None, data=None):
        self.duration = 0
        self.name = name
        self.filename = filename
        if filename:
            inputfile = open(filename, "rb")
        else:
            inputfile = io.BytesIO(data)
        try:
            with self.convertformat(inputfile) as inputfile:
                with wave.open(inputfile, "r") as wavesample:
                    assert wavesample.getframerate() == norm_samplerate
                    assert wavesample.getsampwidth() == norm_samplewidth
                    numchannels = wavesample.getnchannels()
                    assert numchannels in (1, 2)
                    self.sampledata = wavesample.readframes(wavesample.getnframes())
                    self.duration = wavesample.getnframes() / norm_samplerate
            if numchannels == 1 and norm_channels == 2:
                # on the fly conversion to stereo if it is a mono sample
                self.sampledata = audioop.tostereo(self.sampledata, norm_samplewidth, 1, 1)
        except FileNotFoundError as x:
            print(x)
            raise SystemExit("'oggdec' (vorbis-tools) must be installed on your system to hear sounds in this game. "
                             "Or you can start it with the --nosound option.")

    def convertformat(self, stream):
        conversion_required = True
        try:
            # maybe the existing data is already a WAV in the correct format
            with wave.open(stream, "r") as wavesample:
                if wavesample.getframerate() == norm_samplerate and wavesample.getnchannels() in (1, 2) \
                        and wavesample.getsampwidth() == norm_samplewidth:
                    conversion_required = False
        except (wave.Error, IOError):
            conversion_required = True
        finally:
            stream.seek(0, 0)
        if not conversion_required:
            return stream
        # use oggdec to convert the audio file on the fly to a WAV
        uncompress_command = ["oggdec", "--quiet", "--output", "-", "-"]
        with tempfile.NamedTemporaryFile() as tmpfile:
            tmpfile.write(stream.read())
            tmpfile.seek(0, 0)
            converter = subprocess.Popen(uncompress_command, stdin=tmpfile, stdout=subprocess.PIPE)
            return io.BytesIO(converter.stdout.read())


class DummySample(Sample):
    def __init__(self, name, filename=None, duration=0):
        self.name = name
        self.filename = filename
        self.duration = duration
        self.sampledata = b""


class AudioApi:
    def __init__(self):
        self.samplerate = norm_samplerate
        self.samplewidth = norm_samplewidth
        self.nchannels = norm_channels
        self.queue_size = 100

    def __str__(self):
        api_ver = self.query_api_version()
        if api_ver and api_ver != "unknown":
            return self.__class__.__name__ + ", " + self.query_api_version()
        else:
            return self.__class__.__name__

    def close(self):
        pass

    def query_api_version(self):
        return "unknown"

    def play(self, sample):
        raise NotImplementedError

    def stop_currently_playing(self):
        raise NotImplementedError

    def wipe_queue(self):
        pass


class PyAudio(AudioApi):
    """Api to the somewhat older pyaudio library (that uses portaudio)"""
    def __init__(self):
        super().__init__()
        global pyaudio
        import pyaudio
        self.stream = None
        self.stop_sample = False
        self.samp_queue = queue.Queue(maxsize=self.queue_size)

        def audio_thread():
            audio = pyaudio.PyAudio()
            try:
                audio_format = audio.get_format_from_width(self.samplewidth) if self.samplewidth != 4 else pyaudio.paInt32
                self.stream = audio.open(format=audio_format, channels=self.nchannels, rate=self.samplerate, output=True)
                try:
                    while True:
                        sample = self.samp_queue.get()
                        self.stop_sample = False
                        if not sample:
                            break
                        i = 0
                        while i < len(sample.sampledata) and not self.stop_sample:
                            self.stream.write(sample.sampledata[i:i+1024])
                            i += 1024
                finally:
                    self.stream.close()
            finally:
                audio.terminate()

        outputter = threading.Thread(target=audio_thread, name="audio-pyaudio", daemon=True)
        outputter.start()

    def close(self):
        if self.samp_queue:
            self.stop_sample = True
            self.play(None)

    def query_api_version(self):
        return pyaudio.get_portaudio_version_text()

    def play(self, sample):
        self.samp_queue.put(sample)

    def stop_currently_playing(self):
        self.stop_sample = True

    def wipe_queue(self):
        try:
            while True:
                self.samp_queue.get(block=False)
        except queue.Empty:
            pass


class SounddeviceThread(AudioApi):
    """Api to the more featureful sounddevice library (that uses portaudio) -
    using blocking streams with an audio output thread"""
    def __init__(self):
        super().__init__()
        global sounddevice
        import sounddevice
        self.stream = None
        self.samp_queue = queue.Queue(maxsize=self.queue_size)
        self.stop_sample = False
        stream_ready = threading.Event()

        def audio_thread():
            try:
                if self.samplewidth == 1:
                    dtype = "int8"
                elif self.samplewidth == 2:
                    dtype = "int16"
                elif self.samplewidth == 3:
                    dtype = "int24"
                elif self.samplewidth == 4:
                    dtype = "int32"
                else:
                    raise ValueError("invalid sample width")
                self.stream = sounddevice.RawOutputStream(self.samplerate, channels=self.nchannels, dtype=dtype)
                self.stream.start()
                stream_ready.set()
                q = self.samp_queue
                try:
                    while True:
                        sample = q.get()
                        if not sample:
                            break
                        self.stop_sample = False
                        i = 0
                        while i < len(sample.sampledata) and not self.stop_sample:
                            self.stream.write(sample.sampledata[i:i+1024])
                            i += 1024
                finally:
                    # self.stream.stop()  causes pop
                    self.stream.close()
            finally:
                pass

        self.output_thread = threading.Thread(target=audio_thread, name="audio-sounddevice", daemon=True)
        self.output_thread.start()
        stream_ready.wait()

    def close(self):
        if self.samp_queue:
            self.stop_sample = True
            self.play(None)
        if self.output_thread:
            self.output_thread.join()
        sounddevice.stop()

    def query_api_version(self):
        return sounddevice.get_portaudio_version()[1]

    def play(self, sample):
        self.samp_queue.put(sample)

    def stop_currently_playing(self):
        self.stop_sample = True

    def wipe_queue(self):
        try:
            while True:
                self.samp_queue.get(block=False)
        except queue.Empty:
            pass


class Sounddevice(AudioApi):
    """Api to the more featureful sounddevice library (that uses portaudio) -
    using callback stream, without a separate audio output thread"""
    class BufferQueueReader:
        def __init__(self, bufferqueue):
            self.queue_items = self.iter_queue(bufferqueue)
            self.current_item = None
            self.i = 0
            self.queue_empty_event = threading.Event()
            self.stop_sample = False

        def iter_queue(self, bufferqueue):
            while True:
                try:
                    yield bufferqueue.get_nowait()
                except queue.Empty:
                    self.queue_empty_event.set()
                    yield None

        def next_chunk(self, size):
            if self.stop_sample:
                self.stop_sample = False
                self.current_item = None
            if not self.current_item:
                data = next(self.queue_items)
                if not data:
                    return None
                self.current_item = memoryview(data)
                self.i = 0
            rest_current = len(self.current_item) - self.i
            if size <= rest_current:
                # current item still contains enough data
                result = self.current_item[self.i:self.i+size]
                self.i += size
                return result
            # current item is too small, get more data from the queue
            # we assume the size of the chunks in the queue is >= required block size
            data = next(self.queue_items)
            if data:
                result = self.current_item[self.i:].tobytes()
                self.i = size - len(result)
                result += data[0:self.i]
                self.current_item = memoryview(data)
                assert len(result) == size, "queue blocks need to be >= buffersize"
                return result
            else:
                # no new data available, just return the last remaining data from current block
                result = self.current_item[self.i:]
                self.current_item = None
                return result or None

    def __init__(self):
        super().__init__()
        global sounddevice
        import sounddevice
        self.buffer_queue = queue.Queue(maxsize=self.queue_size)
        if self.samplewidth == 1:
            dtype = "int8"
        elif self.samplewidth == 2:
            dtype = "int16"
        elif self.samplewidth == 3:
            dtype = "int24"
        elif self.samplewidth == 4:
            dtype = "int32"
        else:
            raise ValueError("invalid sample width")
        frames_per_chunk = self.samplerate // 20
        self.buffer_queue_reader = Sounddevice.BufferQueueReader(self.buffer_queue)
        self.stream = sounddevice.RawOutputStream(self.samplerate, channels=self.nchannels, dtype=dtype,
            blocksize=frames_per_chunk, callback=self.streamcallback)
        self.stream.start()

    def close(self):
        if self.stream:
            self.buffer_queue_reader.stop_sample = True
            # self.stream.stop()   causes pop
            self.stream.close()
            self.stream = None
        self.buffer_queue = None
        sounddevice.stop()

    def query_api_version(self):
        return sounddevice.get_portaudio_version()[1]

    def play(self, sample):
        self.buffer_queue.put(sample.sampledata)

    def stop_currently_playing(self):
        self.buffer_queue_reader.stop_sample = True

    def wipe_queue(self):
        try:
            while True:
                self.buffer_queue.get(block=False)
        except queue.Empty:
            pass

    def streamcallback(self, outdata, frames, time, status):
        data = self.buffer_queue_reader.next_chunk(len(outdata))
        if not data:
            # no frames available, use silence
            data = b"\0" * len(outdata)
            # raise sounddevice.CallbackAbort   this will abort the stream
        if len(data) < len(outdata):
            # underflow, pad with silence
            outdata[:len(data)] = data
            outdata[len(data):] = b"\0"*(len(outdata)-len(data))
            # raise sounddevice.CallbackStop    this will play the remaining samples and then stop the stream
        else:
            outdata[:] = data


class Winsound(AudioApi):
    """Minimally featured api for the winsound library that comes with Python on Windows."""
    def __init__(self):
        super().__init__()
        import winsound as _winsound
        global winsound
        winsound = _winsound
        self.threads = []
        os.makedirs(os.path.expanduser("~/.bouldercaves"), exist_ok=True)
        oggdecexe = pkgutil.get_data(__name__, "sounds/oggdec.exe")
        with open(os.path.expanduser("~/.bouldercaves/oggdec.exe"), "wb") as exefile:
            exefile.write(oggdecexe)

    def play(self, sample):
        winsound.PlaySound(sample.filename, winsound.SND_ASYNC)

    def stop_currently_playing(self):
        pass

    def store_sample_file(self, filename, data):
        # convert the sample file to a wav file on disk.
        oggfilename = os.path.expanduser("~/.bouldercaves/")+filename
        with open(oggfilename, "wb") as oggfile:
            oggfile.write(data)
        wavfilename = os.path.splitext(oggfilename)[0] + ".wav"
        oggdeccmd = [os.path.expanduser("~/.bouldercaves/oggdec.exe"), "--quiet", oggfilename, "-o", wavfilename]
        subprocess.call(oggdeccmd)
        os.remove(oggfilename)
        return wavfilename


class DummyAudio(AudioApi):
    """Dummy audio api that does nothing"""
    def __init__(self):
        super().__init__()

    def query_api_version(self):
        return "dummy"

    def play(self, sample):
        pass

    def stop_currently_playing(self):
        pass


class Output:
    """Plays samples to audio output device or streams them to a file."""
    def __init__(self, api=None):
        if api is None:
            api = best_api(dummy_enabled=True)
        self.audio_api = api

    def __enter__(self):
        return self

    def __exit__(self, xtype, value, traceback):
        self.close()

    def stop(self):
        self.audio_api.stop_currently_playing()

    def close(self):
        self.audio_api.close()

    def play_sample(self, samplename):
        """Play a single sample (asynchronously)."""
        global samples
        self.audio_api.wipe_queue()   # sounds in queue but not yet played are discarded...
        self.stop()   # and the currently playing sample is stopped so we can play new sounds.
        self.audio_api.play(samples[samplename])

    def wipe_queue(self):
        """Remove all pending samples to be played from the queue"""
        self.audio_api.wipe_queue()


samples = {}
output = None


def init_audio(dummy=False):
    sounds = {
        "music": "bdmusic.ogg",
        "cover": "cover.ogg",
        "crack": "crack.ogg",
        "boulder": "boulder.ogg",
        "finished": "finished.ogg",
        "explosion": "explosion.ogg",
        "extra_life": "bonus_life.ogg",
        "walk_empty": "walk_empty.ogg",
        "walk_dirt": "walk_dirt.ogg",
        "collect_diamond": "collectdiamond.ogg",
        "box_push": "box_push.ogg",
        # "amoeba": "amoeba.ogg",   # @todo not yet used, can't play continous sound + other sounds...
        # "magic_wall": "magic_wall.ogg",  # @todo not yet used, can't play continous sound + other sounds...
        "diamond1": "diamond1.ogg",
        "diamond2": "diamond2.ogg",
        "diamond3": "diamond3.ogg",
        "diamond4": "diamond4.ogg",
        "diamond5": "diamond5.ogg",
        "diamond6": "diamond6.ogg",
        "game_over": "game_over.ogg",
        "timeout1": "timeout1.ogg",
        "timeout2": "timeout2.ogg",
        "timeout3": "timeout3.ogg",
        "timeout4": "timeout4.ogg",
        "timeout5": "timeout5.ogg",
        "timeout6": "timeout6.ogg",
        "timeout7": "timeout7.ogg",
        "timeout8": "timeout8.ogg",
        "timeout9": "timeout9.ogg",
    }

    global output, samples
    if dummy:
        output = Output(DummyAudio())
    else:
        output = Output()
    if isinstance(output.audio_api, DummyAudio):
        if not dummy:
            print("No audio output available. Install 'sounddevice' or 'pyaudio' library to hear things.")
        for name, filename in sounds.items():
            samples[name] = DummySample(name)
        return

    print("Loading sound data...")
    for name, filename in sounds.items():
        data = pkgutil.get_data(__name__, "sounds/" + filename)
        if isinstance(output.audio_api, Winsound):
            # winsound needs the samples as physical WAV files on disk.
            filename = output.audio_api.store_sample_file(filename, data)
            samples[name] = DummySample(name, filename)
        else:
            samples[name] = Sample(name, data=data)
    print("Sound API used:", output.audio_api)
    if isinstance(output.audio_api, Winsound):
        print("Winsound is used as fallback. For better audio, it is recommended to install the 'sounddevice' or 'pyaudio' library instead.")


def shutdown_audio():
    if output:
        output.wipe_queue()
        output.close()


if __name__ == "__main__":
    init_audio()
    for i in range(10):
        print("playing #", i)
        output.play_sample("music")
        time.sleep(2)
    print("STOP CURRENT!")
    output.stop()
    time.sleep(1)
    shutdown_audio()

