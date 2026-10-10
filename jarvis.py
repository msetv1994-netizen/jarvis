"""Jarvis — a local voice assistant that can also scaffold and deploy apps."""

import contextvars
import ctypes
import datetime
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    from google import genai
except ImportError:
    genai = None


GEMINI_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.7-flash",
)
_WAKE_WORDS = {"jarvis", "jarvice", "jarves", "jervis", "जार्विस", "जारविस", "जरविस"}
_LANG = "en"
_VOICES_READY = False
MAX_HEAL_ATTEMPTS = 3
_SHUTDOWN = threading.Event()
_CHILD_PROCESSES: list[subprocess.Popen[str]] = []
_MIC_WARNED = False
_SPEECH_WARNED = False
_SKIP_WINDOW_CLASSES = {
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
    "DummyDWMListenerWindow",
}
WALLPAPER_PATH = "C:/Jarvis/wallpaper.jpg"
APPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps")
_LAST_APP_NAME = ""
_UI = None
# Last 15 user/Jarvis exchanges. Each exchange is two messages, so the cap is 30.
HISTORY_LIMIT = 15
chat_history: list[dict[str, object]] = []
_capture_reply = False
_captured_reply: list[str] = []
_SILENT = False
_API_LOCK = threading.Lock()
_IN_API: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_in_api", default=False)
_API_CONFIRM: contextvars.ContextVar[bool] = contextvars.ContextVar("jarvis_api_confirm", default=False)


def _load_local_env() -> None:
    """Load a local .env without overriding variables already set on the host."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


def attach_ui(ui) -> None:
    global _UI
    _UI = ui


def console(text: str) -> None:
    if sys.stdout is None:
        return
    try:
        print(text)
    except OSError:
        pass


def note(text: str) -> None:
    console(text)
    if _UI is not None:
        _UI.log("sys", text)


def set_status(status: str) -> None:
    if _UI is not None:
        _UI.set_status(status)


def _should_stop() -> bool:
    return _UI is not None and _UI.stopping


def speak(text: str) -> None:
    """Say text aloud and print it."""
    if _capture_reply:
        _captured_reply.append(text)
    console(f"Jarvis: {text}")
    if _SILENT or pyttsx3 is None:
        if _UI is not None and not _SILENT:
            _UI.log("jarvis", text)
            _UI.set_status("SPEAKING")
        return
    if _UI is not None:
        _UI.log("jarvis", text)
        _UI.set_status("SPEAKING")
    if _LANG == "hi" and _speak_hindi(text):
        time.sleep(0.35)
        return
    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()
    engine.stop()
    # Let the room echo die so the mic does not hear Jarvis.
    time.sleep(0.35)


def reply(english: str, hindi: str) -> None:
    """Speak English or Hindi, matching the user's last phrase."""
    speak(hindi if _LANG == "hi" else english)


def _speak_hindi(text: str) -> bool:
    """Play Hindi with Google speech. Return False when that is unavailable."""
    try:
        import tempfile

        from gtts import gTTS
    except ImportError:
        return False
    path = os.path.join(tempfile.gettempdir(), "jarvis-hindi.mp3")
    try:
        gTTS(text=text, lang="hi").save(path)
        return _play_mp3(path)
    except Exception as exc:
        console(f"Hindi voice error: {exc}")
        return False
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _play_mp3(path: str) -> bool:
    winmm = ctypes.windll.winmm
    alias = "jarvishindi"
    winmm.mciSendStringW(f"close {alias}", None, 0, None)
    opened = winmm.mciSendStringW(f'open "{path}" type mpegvideo alias {alias}', None, 0, None)
    if opened != 0:
        return False
    played = winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
    winmm.mciSendStringW(f"close {alias}", None, 0, None)
    return played == 0


def use_language(phrase: str) -> None:
    """Remember whether the latest request was Hindi or English."""
    global _LANG
    if any("\u0900" <= char <= "\u097f" for char in phrase):
        _LANG = "hi"
        return
    hints = (
        "kholo",
        "khol do",
        "banao",
        "bana do",
        "batao",
        "bata do",
        "band karo",
        "chalu",
        "samay",
        "badlo",
        "badal",
        "lagao",
        "jodo",
        "dikhao",
        "kripya",
        "namaste",
        "theek",
        "haan",
    )
    lowered = phrase.lower()
    _LANG = "hi" if any(hint in lowered for hint in hints) else "en"


_COMMAND_PHRASES = (
    ("डेटाबेस के साथ", " with database"),
    ("स्टोरेज के साथ", " with storage"),
    ("सारी विंडो बंद करो", "close all windows"),
    ("सारी विंडोज बंद करो", "close all windows"),
    ("सब विंडो बंद करो", "close all windows"),
    ("sab window band karo", "close all windows"),
    ("saari window band karo", "close all windows"),
    ("windows band karo", "close all windows"),
    ("sab band karo", "close all windows"),
    ("खोलो यूट्यूब", "open youtube"),
    ("यूट्यूब खोलो", "open youtube"),
    ("यूट्यूब खोल दो", "open youtube"),
    ("youtube khol do", "open youtube"),
    ("youtube kholo", "open youtube"),
    ("kholo youtube", "open youtube"),
    ("youtube chalu karo", "open youtube"),
    ("खोलो गूगल", "open google"),
    ("गूगल खोलो", "open google"),
    ("गूगल खोल दो", "open google"),
    ("google khol do", "open google"),
    ("google kholo", "open google"),
    ("kholo google", "open google"),
    ("वॉलपेपर बदलो", "change wallpaper"),
    ("wallpaper badal do", "change wallpaper"),
    ("wallpaper badlo", "change wallpaper"),
    ("theme badlo", "update theme"),
    ("क्या समय हुआ", "what is the time"),
    ("क्या टाइम हुआ", "what is the time"),
    ("समय बताओ", "what is the time"),
    ("टाइम बताओ", "what is the time"),
    ("time batao", "what is the time"),
    ("samay batao", "what is the time"),
    ("kitna baja", "what is the time"),
    ("ऐप बना दो", "create app"),
    ("ऐप बनाओ", "create app"),
    ("app bana do", "create app"),
    ("app banao", "create app"),
    ("application banao", "create app"),
    ("ऐप डिप्लॉय करो", "deploy app"),
    ("deploy karo", "deploy app"),
    ("लाइव करो", "make it live"),
    ("live karo", "make it live"),
    ("डेटाबेस जोड़ो", "add database"),
    ("database jodo", "add database"),
    ("database lagao", "add database"),
    ("स्टोरेज जोड़ो", "add storage"),
    ("storage lagao", "add storage"),
    ("के साथ", " with "),
    ("डेटाबेस", "database"),
    ("स्टोरेज", "storage"),
    ("सुपाबेस", "supabase"),
    ("पोस्टग्रेस", "postgres"),
    ("बंद हो जाओ", "goodbye"),
    ("band ho jao", "goodbye"),
    ("अलविदा", "goodbye"),
    ("alvida", "goodbye"),
    ("चुप हो जाओ", "stop listening"),
)


def normalize_command(phrase: str) -> str:
    """Turn Hindi and Hinglish requests into the English commands Jarvis runs."""
    text = phrase.lower().strip()
    for source, target in _COMMAND_PHRASES:
        text = text.replace(source, target)
    return " ".join(text.split())


def collapse_repeated_phrase(text: str) -> str:
    """Turn "open google open google" into "open google"."""
    words = text.split()
    while len(words) >= 2 and len(words) % 2 == 0:
        half = len(words) // 2
        if words[:half] != words[half:]:
            break
        words = words[:half]
    return " ".join(words)


def listen() -> str:
    """Listen on the microphone and return recognized text. Stay quiet on misses."""
    global _MIC_WARNED, _SPEECH_WARNED
    recognizer = sr.Recognizer()
    recognizer.dynamic_energy_threshold = False
    recognizer.pause_threshold = 0.8
    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.45)
            recognizer.energy_threshold = max(recognizer.energy_threshold * 1.4, 280)
            console("Listening, Sir...")
            audio = recognizer.listen(source, timeout=8, phrase_time_limit=12)
    except sr.WaitTimeoutError:
        return ""
    except OSError:
        if not _MIC_WARNED:
            _MIC_WARNED = True
            reply("Sir, I cannot access the microphone.", "सर, माइक्रोफोन नहीं मिल रहा।")
        return ""

    try:
        query = _transcribe(recognizer, audio)
    except sr.UnknownValueError:
        console("Unclear audio.")
        return ""
    except sr.RequestError:
        if not _SPEECH_WARNED:
            _SPEECH_WARNED = True
            reply("Sir, the speech service is unavailable.", "सर, स्पीच सर्विस अभी उपलब्ध नहीं है।")
        return ""
    console(f"You: {query}")
    _SPEECH_WARNED = False
    return query


def _transcribe(recognizer: sr.Recognizer, audio: sr.AudioData) -> str:
    """Read the same audio in English and Hindi, then keep the clearer phrase."""
    found: list[str] = []
    failures = 0
    for language in ("en-IN", "hi-IN"):
        try:
            text = recognizer.recognize_google(audio, language=language)
        except sr.UnknownValueError:
            continue
        except sr.RequestError:
            failures += 1
            continue
        cleaned = collapse_repeated_phrase(text.lower().strip())
        if cleaned:
            found.append(cleaned)
    if found:
        return max(found, key=_transcript_score)
    if failures:
        raise sr.RequestError("speech service unavailable")
    raise sr.UnknownValueError()


def _transcript_score(text: str) -> int:
    score = min(len(text), 80)
    if any(word in text.split() for word in _WAKE_WORDS):
        score += 50
    if any("\u0900" <= char <= "\u097f" for char in text):
        score += 15
    hints = ("youtube", "google", "time", "create app", "खोलो", "बनाओ", "बताओ", "kholo", "banao", "database", "डेटाबेस")
    if any(hint in text for hint in hints):
        score += 30
    return score


def command_after_wake(phrase: str) -> str | None:
    """Return the command if the user called Jarvis, otherwise None."""
    words = phrase.split()
    fillers = {"hey", "ok", "okay", "hi", "हे", "अरे", "सुनो"}
    if not any(word in _WAKE_WORDS for word in words):
        return None
    kept = [word for word in words if word not in _WAKE_WORDS and word not in fillers]
    return " ".join(kept)


def tell_time() -> None:
    now = datetime.datetime.now().strftime("%I:%M %p")
    reply(f"Sir, the current time is {now}.", f"सर, अभी समय {now} है।")


def open_site(name: str, url: str) -> None:
    reply(f"Opening {name}, Sir.", f"सर, {name} खोल रहा हूँ।")
    webbrowser.open(url)


def change_wallpaper(image_path: str = WALLPAPER_PATH) -> None:
    """Set the Windows desktop wallpaper from an image file."""
    path = os.path.abspath(image_path)
    if not os.path.isfile(path):
        reply("Sir, I cannot find the wallpaper image.", "सर, वॉलपेपर की फाइल नहीं मिली।")
        return

    spi_set_desktop_wallpaper = 20
    update_profile = 0x01
    notify_change = 0x02
    updated = ctypes.windll.user32.SystemParametersInfoW(
        spi_set_desktop_wallpaper,
        0,
        path,
        update_profile | notify_change,
    )
    if updated:
        reply("Wallpaper updated, Sir.", "सर, वॉलपेपर बदल गया।")
    else:
        reply("Sir, I could not change the wallpaper.", "सर, वॉलपेपर नहीं बदल पाया।")


def _window_text(user32, getter, hwnd, size: int = 256) -> str:
    buffer = ctypes.create_unicode_buffer(size)
    getter(hwnd, buffer, size)
    return buffer.value


def _is_cloaked(dwmapi, hwnd) -> bool:
    cloaked = ctypes.c_int()
    result = dwmapi.DwmGetWindowAttribute(
        hwnd,
        14,
        ctypes.byref(cloaked),
        ctypes.sizeof(cloaked),
    )
    return result == 0 and cloaked.value != 0


def visible_app_windows() -> list[int]:
    """Return top-level app windows, skipping the shell and this console."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

    hwnd_type = ctypes.c_void_p
    user32.IsWindowVisible.argtypes = [hwnd_type]
    user32.IsWindowVisible.restype = ctypes.c_bool
    user32.GetWindow.argtypes = [hwnd_type, ctypes.c_uint]
    user32.GetWindow.restype = hwnd_type
    user32.GetClassNameW.argtypes = [hwnd_type, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [hwnd_type]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [hwnd_type, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [hwnd_type, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    user32.PostMessageW.argtypes = [hwnd_type, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.PostMessageW.restype = ctypes.c_bool
    get_exstyle = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    get_exstyle.argtypes = [hwnd_type, ctypes.c_int]
    get_exstyle.restype = ctypes.c_ssize_t
    kernel32.GetConsoleWindow.restype = hwnd_type
    dwmapi.DwmGetWindowAttribute.argtypes = [
        hwnd_type,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    console_hwnd = kernel32.GetConsoleWindow() or 0
    own_pid = os.getpid()
    targets: list[int] = []

    def consider(hwnd, _lparam) -> int:
        try:
            if not hwnd or int(hwnd) == int(console_hwnd):
                return 1
            if not user32.IsWindowVisible(hwnd):
                return 1
            if user32.GetWindow(hwnd, 4):
                return 1
            try:
                if _is_cloaked(dwmapi, hwnd):
                    return 1
            except OSError:
                pass

            class_name = _window_text(user32, user32.GetClassNameW, hwnd)
            if class_name in _SKIP_WINDOW_CLASSES:
                return 1
            if user32.GetWindowTextLengthW(hwnd) == 0:
                return 1

            exstyle = get_exstyle(hwnd, -20)
            tool_window = 0x00000080
            app_window = 0x00040000
            if (exstyle & tool_window) and not (exstyle & app_window):
                return 1

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own_pid:
                return 1
            targets.append(int(hwnd))
        except Exception:
            return 1
        return 1

    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, hwnd_type, hwnd_type)
    callback = callback_type(consider)
    user32.EnumWindows.argtypes = [callback_type, hwnd_type]
    user32.EnumWindows.restype = ctypes.c_bool
    user32.EnumWindows(callback, 0)
    return targets


def close_all_windows() -> None:
    """Ask each visible app window to close, leaving Jarvis itself open."""
    if not confirm_action("Close every visible application window?"):
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.PostMessageW.restype = ctypes.c_bool
    closed = 0
    for hwnd in visible_app_windows():
        if user32.PostMessageW(hwnd, 0x0010, 0, 0):
            closed += 1

    if closed:
        reply(f"Closing {closed} windows, Sir.", f"सर, {closed} विंडो बंद कर रहा हूँ।")
    else:
        reply("Sir, I do not see any windows to close.", "सर, बंद करने के लिए कोई विंडो नहीं है।")


def gemini_failure_kind(exc: Exception) -> str:
    """Classify a Gemini error as auth, retryable capacity, or other."""
    text = str(exc).lower()
    if any(token in text for token in ("401", "403", "api key", "unauthenticated", "permission denied")):
        return "auth"
    retry_tokens = (
        "503",
        "unavailable",
        "429",
        "resource_exhausted",
        "high demand",
        "overloaded",
        "404",
        "not_found",
        "not found",
    )
    if any(token in text for token in retry_tokens):
        return "retry"
    return "other"


def _trim_history() -> None:
    """Keep at most HISTORY_LIMIT exchanges, and never start on a Jarvis line."""
    max_messages = HISTORY_LIMIT * 2
    if len(chat_history) > max_messages:
        del chat_history[: len(chat_history) - max_messages]
    if chat_history and chat_history[0].get("role") == "model":
        del chat_history[0]


def remember_exchange(user_text: str, jarvis_text: str) -> None:
    """Store one finished exchange and drop the oldest when the list is full."""
    user_text = " ".join(user_text.split())
    jarvis_text = " ".join(jarvis_text.split())
    if not user_text or not jarvis_text:
        return
    chat_history.append({"role": "user", "parts": [{"text": user_text}]})
    chat_history.append({"role": "model", "parts": [{"text": jarvis_text}]})
    _trim_history()


def ask_gemini(prompt: str) -> None:
    """Answer with Gemini when the request is not a local command."""
    if genai is None:
        reply(
            "Sir, google-genai is not installed, so I cannot answer that.",
            "सर, google-genai इंस्टॉल नहीं है, इसलिए यह जवाब नहीं दे सकता।",
        )
        return

    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )
    if not api_key:
        reply(
            "Sir, set the GEMINI_API_KEY environment variable so I can use Gemini.",
            "सर, Gemini के लिए GEMINI_API_KEY सेट करें।",
        )
        return

    set_status("THINKING")
    client = genai.Client(api_key=api_key)
    language = "Hindi" if _LANG == "hi" else "English"
    chat_history.append({"role": "user", "parts": [{"text": prompt}]})
    _trim_history()
    contents = list(chat_history)
    config = {
        "system_instruction": (
            "You are Jarvis, a concise voice assistant. "
            "Address the user as Sir. Reply in one or two short sentences. "
            f"Reply only in {language}. "
            "Use the earlier turns in this conversation. "
            "Remember names, choices, and app details the user already told you, "
            "and use them when answering or when they matter to the next task."
        )
    }
    last_error = None
    failures = 0
    answered = False
    try:
        for model in GEMINI_MODELS:
            if failures >= MAX_HEAL_ATTEMPTS or _SHUTDOWN.is_set():
                break
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
                answer = (getattr(response, "text", None) or "").strip()
                if not answer:
                    reply("Sir, Gemini returned an empty response.", "सर, Gemini ने खाली जवाब दिया।")
                    return
                chat_history.append({"role": "model", "parts": [{"text": answer}]})
                _trim_history()
                answered = True
                speak(answer)
                return
            except Exception as exc:
                failures += 1
                last_error = exc
                kind = gemini_failure_kind(exc)
                note(f"Gemini error ({model}): {exc}")
                if kind != "retry":
                    reply("Sir, I could not reach Gemini just now.", "सर, Gemini अभी नहीं मिल पाया।")
                    return
                if failures >= MAX_HEAL_ATTEMPTS:
                    note(f"Stopped after {failures} failed attempts: {last_error}")
                    reply(
                        "Sir, I stopped after 3 failed attempts so this would not keep looping.",
                        "सर, 3 बार असफल रहने के बाद मैं रुक गया, ताकि यह बार-बार न चले।",
                    )
                    return
                time.sleep(1.2)

        note(f"Gemini gave up: {last_error}")
        reply(
            "Sir, Gemini is busy right now. Please try again in a moment.",
            "सर, Gemini अभी व्यस्त है। थोड़ी देर बाद फिर कहिए।",
        )
    finally:
        if not answered and chat_history and chat_history[-1].get("role") == "user":
            chat_history.pop()


def is_kill_switch(phrase: str) -> bool:
    text = " ".join(phrase.lower().split())
    return "stop jarvis" in text or "emergency shutdown" in text


def _is_approval(phrase: str) -> bool:
    text = " ".join(phrase.lower().split())
    approvals = {
        "yes",
        "yes sir",
        "yes proceed",
        "proceed",
        "allow",
        "go ahead",
        "do it",
        "haan",
        "haan sir",
        "ha",
        "हाँ",
        "हाँ सर",
        "हां",
        "हां सर",
    }
    return text in approvals


def confirm_action(action: str) -> bool:
    """Pause for Allow/Deny, or a spoken yes, before a guarded action."""
    if _SHUTDOWN.is_set():
        return False
    if _IN_API.get():
        allowed = bool(_API_CONFIRM.get())
    elif _UI is not None:
        allowed = _UI.confirm(action)
    else:
        reply(
            f"Sir, may I do this? Say yes or proceed. {action}",
            f"सर, क्या मैं यह करूँ? हाँ या proceed बोलिए। {action}",
        )
        phrase = listen()
        if is_kill_switch(phrase):
            emergency_shutdown()
        allowed = _is_approval(phrase)
    if not allowed:
        if _IN_API.get():
            reply(
                "Cancelled, Sir. Turn on Allow and send that request again.",
                "सर, रद्द किया। Allow चालू करके फिर भेजिए।",
            )
        else:
            reply("Cancelled, Sir.", "सर, रद्द किया।")
    return allowed


def emergency_shutdown() -> None:
    """Stop child processes, background work, and the window, then exit."""
    _SHUTDOWN.set()
    reply("Emergency shutdown. Stopping everything, Sir.", "इमरजेंसी शटडाउन। सब कुछ रोक रहा हूँ, सर।")
    for process in list(_CHILD_PROCESSES):
        try:
            process.terminate()
        except OSError:
            pass
    try:
        from system_stats import stop_system_monitor

        stop_system_monitor()
    except Exception:
        pass
    if _UI is not None:
        _UI.force_stop()
    os._exit(0)


def run_command(args: list[str], cwd: str) -> subprocess.CompletedProcess[str] | None:
    """Run a terminal command only after the user allows it."""
    shown = " ".join(args)
    if not confirm_action(f"Run this terminal command?\n{shown}"):
        return None
    if _SHUTDOWN.is_set():
        return None
    process = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
    )
    _CHILD_PROCESSES.append(process)
    stdout, stderr = "", ""
    try:
        deadline = time.time() + 180
        while process.poll() is None:
            if _SHUTDOWN.is_set() or time.time() >= deadline:
                process.terminate()
                break
            time.sleep(0.2)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
    finally:
        if process in _CHILD_PROCESSES:
            _CHILD_PROCESSES.remove(process)
    return subprocess.CompletedProcess(
        args,
        process.returncode if process.returncode is not None else 1,
        stdout or "",
        stderr or "",
    )


def safe_app_name(raw: str) -> str:
    slug = re.sub(r"[^\w]+", "-", raw.lower(), flags=re.UNICODE).strip("-")
    if slug in {"con", "prn", "aux", "nul"} or slug.startswith(("con.", "prn.", "aux.", "nul.")):
        return ""
    return slug[:32]


def storage_choice(command: str) -> str:
    """Pick sqlite, supabase, or postgres when the user asks for storage."""
    if "supabase" in command:
        return "supabase"
    if "postgres" in command or "postgresql" in command:
        return "postgres"
    if any(word in command for word in ("storage", "database", "sqlite")):
        return "sqlite"
    return ""


def parse_app_name(command: str) -> str:
    rest = command
    for prefix in ("create an app", "create app", "make an app", "make app"):
        if prefix in rest:
            rest = rest.split(prefix, 1)[1]
            break
    for marker in (" with ", " using ", " and a ", " and "):
        if marker in rest:
            rest = rest.split(marker, 1)[0]
    return safe_app_name(rest)


def parse_deploy_name(command: str) -> str:
    rest = command
    for prefix in ("deploy app", "deploy the app", "make it live"):
        if prefix in rest:
            rest = rest.split(prefix, 1)[1]
            break
    return safe_app_name(rest) or _LAST_APP_NAME


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _frontend_files(name: str, title: str) -> dict[str, str]:
    return {
        "frontend/index.html": f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <main>
    <p class="mark">J.A.R.V.I.S.</p>
    <h1>{title}</h1>
    <p id="status">Connecting...</p>
    <form id="record-form">
      <input id="token" type="password" placeholder="Access token" autocomplete="off">
      <input id="content" name="content" placeholder="Add a record" required>
      <button type="submit">Save</button>
    </form>
    <ul id="records"></ul>
  </main>
  <script src="app.js"></script>
</body>
</html>
""",
        "frontend/style.css": """body {
  margin: 0;
  min-height: 100vh;
  font-family: "Segoe UI", sans-serif;
  background: #071018;
  color: #e7f7ff;
}
main { max-width: 640px; margin: 0 auto; padding: 48px 20px; }
.mark { letter-spacing: 0.28em; color: #67d7f0; font-size: 12px; }
form { display: flex; gap: 8px; margin: 24px 0; }
input, button {
  border: 1px solid #1c6f8a;
  background: #0c1c26;
  color: #e7f7ff;
  padding: 10px 12px;
}
button { background: #123f52; cursor: pointer; }
li { margin: 8px 0; }
""",
        "frontend/app.js": """const status = document.querySelector("#status");
const list = document.querySelector("#records");
const form = document.querySelector("#record-form");
const tokenInput = document.querySelector("#token");
const api = localStorage.getItem("jarvisApi") || "http://127.0.0.1:8000";

tokenInput.value = sessionStorage.getItem("jarvisToken") || "";

function authHeaders(extra) {
  const token = tokenInput.value.trim();
  sessionStorage.setItem("jarvisToken", token);
  return Object.assign({ Authorization: "Bearer " + token }, extra || {});
}

async function readJson(response) {
  if (response.status === 401) {
    status.textContent = "Unauthorized";
    list.innerHTML = "";
    return null;
  }
  return response.json();
}

async function refresh() {
  const health = await fetch(api + "/mobile", { headers: authHeaders() });
  const body = await readJson(health);
  if (!body) return;
  status.textContent = body.app + " is " + body.status;
  const records = await fetch(api + "/api/records", { headers: authHeaders() });
  const items = await readJson(records);
  if (items === null) return;
  list.innerHTML = items.map((item) => "<li>" + item.content + "</li>").join("");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = new FormData(form).get("content");
  const saved = await fetch(api + "/api/records", {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ content }),
  });
  if (!(await readJson(saved))) return;
  form.reset();
  tokenInput.value = sessionStorage.getItem("jarvisToken") || "";
  refresh();
});

refresh().catch(() => {
  status.textContent = "Start the backend, then reload this page.";
});
""",
    }


def _sqlite_backend(name: str) -> dict[str, str]:
    return {
        "backend/database.py": """import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "app.db")


def connect():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with connect() as connection:
        connection.execute(
            \"\"\"
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            \"\"\"
        )


def list_records():
    with connect() as connection:
        rows = connection.execute(
            "SELECT id, content, created_at FROM records ORDER BY id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def add_record(content: str):
    with connect() as connection:
        cursor = connection.execute("INSERT INTO records (content) VALUES (?)", (content,))
        record_id = cursor.lastrowid
    return {"id": record_id, "content": content}
""",
        "backend/requirements.txt": "fastapi\nuvicorn\n",
    }


def _cloud_backend(storage: str) -> dict[str, str]:
    engine = "Supabase" if storage == "supabase" else "PostgreSQL"
    return {
        "backend/database.py": f"""import json
import os
from urllib import request

ENGINE = "{engine}"
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")


def connection_status():
    if SUPABASE_URL and SUPABASE_KEY:
        return "supabase"
    if DATABASE_URL:
        return "postgres"
    return "not configured"


def list_records():
    if not (SUPABASE_URL and SUPABASE_KEY):
        return []
    url = SUPABASE_URL.rstrip("/") + "/rest/v1/records?select=id,content,created_at&order=id.desc"
    req = request.Request(url, headers=_headers())
    with request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())


def add_record(content: str):
    if DATABASE_URL and not SUPABASE_URL:
        return _add_with_postgres(content)
    url = SUPABASE_URL.rstrip("/") + "/rest/v1/records"
    payload = json.dumps({{"content": content}}).encode()
    req = request.Request(url, data=payload, method="POST", headers=_headers())
    with request.urlopen(req, timeout=20) as response:
        rows = json.loads(response.read().decode())
    return rows[0] if rows else {{"content": content}}


def _headers():
    return {{
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }}


def _add_with_postgres(content: str):
    import psycopg

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                \"\"\"
                CREATE TABLE IF NOT EXISTS records (
                    id bigint generated always as identity primary key,
                    content text not null,
                    created_at timestamptz default now()
                )
                \"\"\"
            )
            cursor.execute(
                "INSERT INTO records (content) VALUES (%s) RETURNING id, content",
                (content,),
            )
            row = cursor.fetchone()
    return {{"id": row[0], "content": row[1]}}
""",
        "backend/requirements.txt": "fastapi\nuvicorn\npsycopg[binary]\n",
        "backend/.env.example": "SUPABASE_URL=\nSUPABASE_KEY=\nDATABASE_URL=\nJARVIS_SECRET_TOKEN=\n",
    }


def _backend_files(name: str, title: str, storage: str) -> dict[str, str]:
    if storage == "sqlite":
        storage_import = "from database import add_record, init_db, list_records\n"
        startup = "init_db()\n"
    elif storage in {"supabase", "postgres"}:
        storage_import = "from database import add_record, list_records\n"
        startup = ""
    else:
        storage_import = ""
        startup = ""
    records_routes = ""
    if storage:
        records_routes = """
@app.get("/api/records")
def get_records():
    return list_records()


@app.post("/api/records")
def post_record(payload: dict):
    content = str(payload.get("content", "")).strip()
    if not content:
        return {"error": "content is required"}
    return add_record(content)
"""
    else:
        records_routes = """
@app.get("/api/records")
def get_records():
    return []


@app.post("/api/records")
def post_record(payload: dict):
    return {"content": str(payload.get("content", "")).strip(), "stored": False}
"""
    main = f"""import os
import secrets
import sys
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent))
{storage_import}

def require_owner(request: Request) -> None:
    \"\"\"Allow the request only when it carries JARVIS_SECRET_TOKEN.\"\"\"
    expected = os.environ.get("JARVIS_SECRET_TOKEN", "")
    provided = _bearer_token(request)
    if not expected or not provided:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-jarvis-token", "").strip()


app = FastAPI(title={title!r}, dependencies=[Depends(require_owner)])
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
{startup}

@app.get("/mobile")
def mobile():
    return {{"app": {name!r}, "status": "online", "access": "owner"}}


@app.get("/api/health")
def health():
    return {{"app": {name!r}, "status": "online"}}
{records_routes}
"""
    node = f"""const crypto = require("crypto");
const http = require("http");
const fs = require("fs");
const path = require("path");

function authorized(req) {{
  const expected = process.env.JARVIS_SECRET_TOKEN || "";
  const header = String(req.headers.authorization || "");
  let provided = "";
  if (header.toLowerCase().startsWith("bearer ")) {{
    provided = header.slice(7).trim();
  }}
  if (!provided) {{
    provided = String(req.headers["x-jarvis-token"] || "").trim();
  }}
  const left = Buffer.from(provided);
  const right = Buffer.from(expected);
  if (!expected || left.length !== right.length) {{
    return false;
  }}
  return crypto.timingSafeEqual(left, right);
}}

const root = path.join(__dirname, "..", "frontend");
const types = {{
  ".html": "text/html",
  ".js": "text/javascript",
  ".css": "text/css",
}};

const server = http.createServer((req, res) => {{
  const pathOnly = (req.url || "/").split("?")[0];
  const needsToken = pathOnly === "/mobile" || pathOnly.startsWith("/api/");
  if (needsToken && !authorized(req)) {{
    res.writeHead(401, {{ "Content-Type": "application/json" }});
    res.end(JSON.stringify({{ detail: "Unauthorized" }}));
    return;
  }}
  if (pathOnly === "/mobile") {{
    res.writeHead(200, {{ "Content-Type": "application/json" }});
    res.end(JSON.stringify({{ app: {name!r}, status: "online", access: "owner" }}));
    return;
  }}
  if (pathOnly === "/api/health") {{
    res.writeHead(200, {{ "Content-Type": "application/json" }});
    res.end(JSON.stringify({{ app: {name!r}, status: "online" }}));
    return;
  }}
  const requested = req.url === "/" ? "index.html" : req.url.split("?")[0].replace(/^\\//, "");
  const filePath = path.resolve(root, requested);
  const safeRoot = path.resolve(root) + path.sep;
  if (!filePath.startsWith(safeRoot) || !fs.existsSync(filePath)) {{
    res.writeHead(404);
    res.end("Not found");
    return;
  }}
  res.writeHead(200, {{ "Content-Type": types[path.extname(filePath)] || "text/plain" }});
  res.end(fs.readFileSync(filePath));
}});

server.listen(3000, () => console.log("{title} node server on http://127.0.0.1:3000"));
"""
    files = {
        "backend/main.py": main,
        "backend/server.js": node,
        "backend/package.json": json_package(name),
        "README.md": readme(name, title, storage),
        "vercel.json": """{
  "version": 2,
  "builds": [{ "src": "frontend/**", "use": "@vercel/static" }],
  "routes": [{ "src": "/(.*)", "dest": "/frontend/$1" }]
}
""",
        "render.yaml": f"""services:
  - type: web
    name: {name}
    runtime: python
    buildCommand: pip install -r backend/requirements.txt
    startCommand: uvicorn backend.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: JARVIS_SECRET_TOKEN
        sync: false
""",
    }
    if storage == "sqlite":
        files.update(_sqlite_backend(name))
    elif storage in {"supabase", "postgres"}:
        files.update(_cloud_backend(storage))
    elif "backend/requirements.txt" not in files:
        files["backend/requirements.txt"] = "fastapi\nuvicorn\n"
    example = files.get("backend/.env.example", "")
    if "JARVIS_SECRET_TOKEN=" not in example:
        files["backend/.env.example"] = example + "JARVIS_SECRET_TOKEN=\n"
    return files


def json_package(name: str) -> str:
    return (
        '{\n'
        f'  "name": "{name}",\n'
        '  "private": true,\n'
        '  "scripts": {\n'
        '    "start": "node backend/server.js"\n'
        '  }\n'
        '}\n'
    )


def readme(name: str, title: str, storage: str) -> str:
    storage_line = storage or "none"
    return f"""# {title}

Created by Jarvis.

## Run the Python API

```
pip install -r backend/requirements.txt
uvicorn backend.main:app --reload
```

Run that command from the app folder.

## Run the Node server

```
npm start
```

Storage: {storage_line}

## Lock the API

Set `JARVIS_SECRET_TOKEN` in the server environment before you deploy.
Do not commit the token. Every `/mobile` and `/api` request must send it:

```
Authorization: Bearer <your token>
```

A missing or wrong token gets `401 Unauthorized`.
`X-Jarvis-Token` is accepted as well.
"""


def create_project(name: str, storage: str) -> str:
    """Create the app folder and return its path."""
    global _LAST_APP_NAME
    folder = os.path.join(APPS_DIR, name)
    os.makedirs(folder, exist_ok=True)
    title = name.replace("-", " ").title()
    files = {}
    files.update(_frontend_files(name, title))
    files.update(_backend_files(name, title, storage))
    for relative, content in files.items():
        _write(os.path.join(folder, relative), content)
    if storage == "sqlite":
        database_file = os.path.join(folder, "backend", "app.db")
        connection = sqlite3.connect(database_file)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.commit()
        connection.close()
    _LAST_APP_NAME = name
    return folder


def create_app_from_command(command: str) -> None:
    name = parse_app_name(command)
    if not name:
        reply(
            "Sir, tell me the app name. For example, create app notes.",
            "सर, ऐप का नाम बताइए। जैसे, Jarvis notes ऐप बनाओ।",
        )
        return
    folder = os.path.join(APPS_DIR, name)
    if os.path.isdir(folder):
        reply(f"Sir, {name} already exists.", f"सर, {name} पहले से बना है।")
        return
    storage = storage_choice(command)
    if not confirm_action(f"Create the app folder '{name}' and write its project files?"):
        return
    set_status("THINKING")
    reply(f"Creating {name}, Sir.", f"सर, {name} बना रहा हूँ।")
    path = create_project(name, storage)
    if storage == "sqlite":
        detail_en = "I added a local SQLite database."
        detail_hi = "लोकल SQLite डेटाबेस जोड़ दिया है।"
    elif storage == "supabase":
        detail_en = "I generated the Supabase connection. Add your keys to backend slash .env.example."
        detail_hi = "Supabase कनेक्शन लिख दिया है। backend की .env.example में अपनी keys डालिए।"
    elif storage == "postgres":
        detail_en = "I generated the Postgres connection. Set DATABASE_URL before you start it."
        detail_hi = "Postgres कनेक्शन लिख दिया है। शुरू करने से पहले DATABASE_URL सेट कीजिए।"
    else:
        detail_en = "Frontend, Node, and FastAPI are ready."
        detail_hi = "Frontend, Node और FastAPI तैयार हैं।"
    note(f"App created at {path}")
    note("Set JARVIS_SECRET_TOKEN on the server. Phone requests without that Bearer token get 401.")
    reply(f"{name} is ready. {detail_en}", f"{name} तैयार है। {detail_hi}")


def add_storage_from_command(command: str) -> None:
    name = _LAST_APP_NAME
    if not name:
        reply("Sir, create an app first.", "सर, पहले एक ऐप बनाइए।")
        return
    folder = os.path.join(APPS_DIR, name)
    if not os.path.isdir(folder):
        reply("Sir, I cannot find the last app.", "सर, पिछला ऐप नहीं मिला।")
        return
    storage = storage_choice(command) or "sqlite"
    if not confirm_action(f"Modify the files in '{name}' to add {storage} storage?"):
        return
    title = name.replace("-", " ").title()
    files = _backend_files(name, title, storage)
    for relative, content in files.items():
        if relative.startswith("backend/") or relative in {"render.yaml", "README.md"}:
            _write(os.path.join(folder, relative), content)
    if storage == "sqlite":
        create_project_db = os.path.join(folder, "backend", "app.db")
        connection = sqlite3.connect(create_project_db)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, content TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.commit()
        connection.close()
        reply(f"SQLite storage is ready for {name}, Sir.", f"सर, {name} का SQLite स्टोरेज तैयार है।")
        return
    label = "Supabase" if storage == "supabase" else "Postgres"
    reply(
        f"{label} connection code is ready for {name}, Sir. Add the keys before starting it.",
        f"सर, {name} के लिए {label} कनेक्शन तैयार है। शुरू करने से पहले keys डालिए।",
    )


def _deploy_url(output: str) -> str:
    match = re.search(r"https://\S+", output)
    if not match:
        return ""
    return match.group(0).rstrip(").,")


def deploy_app_from_command(command: str) -> None:
    name = parse_deploy_name(command)
    if not name:
        reply("Sir, create an app before asking me to deploy it.", "सर, डिप्लॉय से पहले ऐप बनाइए।")
        return
    folder = os.path.join(APPS_DIR, name)
    if not os.path.isdir(folder):
        reply(f"Sir, I cannot find {name}.", f"सर, {name} नहीं मिला।")
        return
    production = "make it live" in command or "prod" in command
    reply(f"Deploying {name}, Sir.", f"सर, {name} डिप्लॉय कर रहा हूँ।")
    set_status("THINKING")
    vercel = shutil.which("vercel")
    render = shutil.which("render")
    if vercel:
        args = ["vercel", "deploy", "--yes"]
        if production:
            args.append("--prod")
        try:
            result = run_command(args, folder)
        except (OSError, subprocess.TimeoutExpired) as exc:
            note(f"Vercel deploy failed: {exc}")
            reply("Sir, Vercel did not finish the deploy.", "सर, Vercel डिप्लॉय पूरा नहीं कर पाया।")
            return
        if result is None:
            return
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        note(output[-1200:])
        url = _deploy_url(output)
        if result.returncode == 0 and url:
            reply(f"{name} is online, Sir. {url}", f"सर, {name} लाइव है। {url}")
            return
        reply(
            "Sir, Vercel needs you to run vercel login in that app folder, then ask me again.",
            "सर, उस ऐप फोल्डर में vercel login चलाकर फिर कहिए।",
        )
        return
    if render:
        try:
            result = run_command(["render", "blueprint", "launch"], folder)
        except (OSError, subprocess.TimeoutExpired) as exc:
            note(f"Render deploy failed: {exc}")
            reply("Sir, Render did not finish the launch.", "सर, Render लॉन्च पूरा नहीं कर पाया।")
            return
        if result is None:
            return
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        note(output[-1200:])
        if result.returncode == 0:
            reply(f"Render accepted {name}, Sir.", f"सर, Render ने {name} स्वीकार कर लिया।")
            return
        reply(
            "Sir, Render needs a login. Run render login, then say deploy app again.",
            "सर, Render के लिए render login चलाकर फिर डिप्लॉय बोलिए।",
        )
        return
    reply(
        "Sir, install the Vercel CLI or Render CLI, then say deploy app again.",
        "सर, Vercel CLI या Render CLI इंस्टॉल करके फिर डिप्लॉय बोलिए।",
    )


def handle_command(command: str) -> bool:
    """Run a command. Return False when the assistant should exit."""
    global _capture_reply
    if not command:
        return True

    _captured_reply.clear()
    _capture_reply = True
    asked_gemini = False
    keep_running = True
    try:
        if any(word in command for word in ("exit", "quit", "goodbye", "stop listening")):
            reply("Goodbye, Sir.", "अलविदा सर।")
            keep_running = False
        elif "create app" in command or "create an app" in command or "make an app" in command or command.startswith("make app"):
            create_app_from_command(command)
        elif "deploy app" in command or "deploy the app" in command or "make it live" in command:
            deploy_app_from_command(command)
        elif any(phrase in command for phrase in ("add database", "add storage", "setup database", "set up database")):
            add_storage_from_command(command)
        elif "close all" in command or "close every window" in command or command == "close windows":
            close_all_windows()
        elif "youtube" in command:
            open_site("YouTube", "https://www.youtube.com")
        elif "google" in command and "gemini" not in command:
            open_site("Google", "https://www.google.com")
        elif "time" in command:
            tell_time()
        elif "change wallpaper" in command or "update theme" in command:
            change_wallpaper()
        else:
            ask_gemini(command)
            asked_gemini = True
    finally:
        _capture_reply = False
        if not asked_gemini:
            spoken = " ".join(_captured_reply).strip()
            if spoken:
                remember_exchange(command, spoken)
    return keep_running


def voice_loop() -> None:
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:
        pass
    if _UI is not None:
        _UI.log("jarvis", "Standing by. Say Jarvis, ya bolo Jarvis.")
    set_status("ONLINE")
    waiting_for_order = False
    while not _should_stop():
        set_status("LISTENING" if waiting_for_order else "ONLINE")
        phrase = listen()
        if _should_stop():
            break
        if not phrase:
            waiting_for_order = False
            continue
        if is_kill_switch(phrase):
            emergency_shutdown()
        if waiting_for_order:
            command = phrase
            waiting_for_order = False
        else:
            command = command_after_wake(phrase)
            if command is None:
                continue
        if _UI is not None:
            _UI.log("you", phrase)
        use_language(phrase)
        command = normalize_command(command)
        if not command:
            waiting_for_order = True
            reply("Yes, Sir?", "हाँ सर।")
            continue
        if not handle_command(command):
            break
    if _UI is not None:
        _UI.request_close()


def _report_startup_error(exc: BaseException) -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis.log")
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.datetime.now().isoformat()} {exc}\n")
    except OSError:
        pass
    ctypes.windll.user32.MessageBoxW(None, str(exc), "Jarvis", 0x10)


def _relaunch_without_console() -> bool:
    """Leave the black console behind and open the reactor window on its own."""
    if os.environ.get("JARVIS_GUI") == "1":
        return False
    if not ctypes.windll.kernel32.GetConsoleWindow():
        return False
    folder = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    env["JARVIS_GUI"] = "1"
    candidates = [
        os.path.join(os.path.dirname(sys.executable), "pythonw.exe"),
        "pythonw",
    ]
    for windowed in candidates:
        if windowed != "pythonw" and not os.path.isfile(windowed):
            continue
        try:
            subprocess.Popen([windowed, os.path.abspath(__file__)], cwd=folder, env=env)
            return True
        except OSError:
            continue
    return False


def answer_voice(text: str, confirmed: bool = False) -> str:
    """Run one command from the phone and return the words Jarvis would say."""
    global _capture_reply, _SILENT
    cleaned = " ".join(text.split())
    if not cleaned:
        return ""
    if is_kill_switch(cleaned):
        return "This cloud service stays online, Sir."
    with _API_LOCK:
        _captured_reply.clear()
        previous_capture = _capture_reply
        previous_silent = _SILENT
        _capture_reply = True
        _SILENT = True
        api_flag = _IN_API.set(True)
        confirm_flag = _API_CONFIRM.set(bool(confirmed))
        try:
            use_language(cleaned)
            command = normalize_command(cleaned)
            if not command:
                reply("Yes, Sir?", "हाँ सर।")
            else:
                handle_command(command)
            return " ".join(_captured_reply).strip() or "Done, Sir."
        finally:
            _IN_API.reset(api_flag)
            _API_CONFIRM.reset(confirm_flag)
            _capture_reply = previous_capture
            _SILENT = previous_silent
            _captured_reply.clear()


def _transcribe_audio(data: bytes, suffix: str) -> str:
    if sr is None or suffix.lower() not in {".wav", ".aiff", ".aif", ".flac"}:
        return ""
    recognizer = sr.Recognizer()
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            temp_path = handle.name
        with sr.AudioFile(temp_path) as source:
            audio = recognizer.record(source)
        return recognizer.recognize_google(audio)
    except Exception:
        return ""
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _build_api():
    """Token-locked FastAPI service for the phone."""
    from fastapi import Depends, FastAPI, HTTPException, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse

    interface = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")

    def require_owner(request: Request) -> None:
        expected = os.environ.get("JARVIS_SECRET_TOKEN", "")
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            provided = header[7:].strip()
        else:
            provided = request.headers.get("x-jarvis-token", "").strip()
        if not expected or not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")

    api = FastAPI(title="Jarvis")
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["https://msetv1994-netizen.github.io", "https://jarvis-wnnw.onrender.com"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Jarvis-Token"],
    )

    @api.get("/")
    @api.get("/mobile")
    def mobile_page():
        if not os.path.isfile(interface):
            raise HTTPException(status_code=404, detail="Mobile interface is missing")
        return FileResponse(interface, media_type="text/html")

    @api.post("/api/voice", dependencies=[Depends(require_owner)])
    async def voice_request(request: Request):
        content_type = request.headers.get("content-type", "")
        text = ""
        confirmed = False
        if "application/json" in content_type:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="JSON object required")
            text = str(payload.get("text") or payload.get("command") or "").strip()
            confirmed = bool(payload.get("confirm"))
        elif "multipart/form-data" in content_type:
            form = await request.form()
            text = str(form.get("text") or "").strip()
            confirmed = str(form.get("confirm") or "").lower() in {"1", "true", "yes", "on"}
            upload = form.get("audio")
            if upload is not None and hasattr(upload, "read") and not text:
                filename = str(getattr(upload, "filename", "") or "voice.wav")
                suffix = os.path.splitext(filename)[1] or ".wav"
                data = await upload.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Voice upload is too large")
                text = _transcribe_audio(data, suffix)
        else:
            raise HTTPException(status_code=400, detail="Send JSON or a voice upload")
        if not text:
            raise HTTPException(status_code=400, detail="No voice command was heard")
        if len(text) > 2000:
            raise HTTPException(status_code=400, detail="Command is too long")
        return {"heard": text, "reply": answer_voice(text, confirmed)}

    return api


try:
    app = _build_api()
except ImportError:
    app = None


def main() -> None:
    try:
        from reactor_ui import ReactorApp

        app = ReactorApp(voice_loop)
        attach_ui(app)
        app.run()
    except Exception as exc:
        _report_startup_error(exc)


if __name__ == "__main__":
    if _relaunch_without_console():
        sys.exit(0)
    main()
