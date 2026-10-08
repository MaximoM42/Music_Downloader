"""Small graphical downloader for YouTube audio you are authorized to save."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
import ctypes
import queue
import re
import shutil
import sys
import threading
from pathlib import Path
from tkinter import filedialog
from urllib.parse import parse_qs, urlparse
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

ANSI_ESCAPE_PATTERN = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def is_youtube_url(value: str) -> bool:
    """Return whether value points to YouTube or a YouTube subdomain."""
    try:
        parsed = urlparse(value.strip())
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False

    return (
        parsed.scheme in {"http", "https"}
        and (host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"))
    )


def is_playlist_url(value: str) -> bool:
    """Return whether the URL explicitly identifies a YouTube playlist."""
    try:
        parsed = urlparse(value.strip())
        query = parse_qs(parsed.query)
    except ValueError:
        return False
    return parsed.path.rstrip("/").endswith("/playlist") or bool(query.get("list"))


def video_id_from_track(track: dict[str, object]) -> str | None:
    """Get the stable video ID from its URL, falling back to extractor metadata."""
    value = track.get("url")
    if value:
        try:
            parsed = urlparse(str(value))
            host = (parsed.hostname or "").lower()
            if host == "youtu.be":
                video_id = parsed.path.strip("/").split("/", maxsplit=1)[0]
                if video_id:
                    return video_id
            query_id = parse_qs(parsed.query).get("v")
            if query_id:
                return query_id[0]
            path_parts = parsed.path.strip("/").split("/")
            if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed", "live"}:
                return path_parts[1]
        except ValueError:
            pass

    video_id = track.get("id")
    return str(video_id) if video_id else None


def downloaded_video_ids(destination: Path) -> set[str]:
    """Find YouTube IDs embedded in downloaded MP3 filenames in destination."""
    return {
        match.group(1)
        for path in destination.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".mp3"
        and (match := re.search(r"\[([A-Za-z0-9_-]+)\]$", path.stem))
    }


def clean_error_message(value: str) -> str:
    """Remove terminal color escapes before displaying downloader errors in Tk."""
    return ANSI_ESCAPE_PATTERN.sub("", value).strip()


def find_javascript_runtimes() -> dict[str, dict[str, str]]:
    """Return yt-dlp runtime settings for supported JavaScript engines on PATH."""
    runtimes: dict[str, dict[str, str]] = {}
    for runtime, command in (
        ("deno", "deno"),
        ("node", "node"),
        ("quickjs", "qjs"),
        ("bun", "bun"),
    ):
        executable = shutil.which(command)
        if executable:
            runtimes[runtime] = {"path": executable}
    return runtimes


def desktop_folder_path(folder_name: str) -> Path:
    """Return the requested folder path on the user's Desktop."""
    name = folder_name.strip()
    invalid_characters = '<>:"/\\|?*'
    reserved_names = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    device_name = name.split(".", maxsplit=1)[0].upper()

    if (
        not name
        or name in {".", ".."}
        or name.endswith((".", " "))
        or any(character in invalid_characters or ord(character) < 32 for character in name)
        or device_name in reserved_names
    ):
        raise ValueError("Escribe un nombre de carpeta válido para Windows.")

    return Path.home() / "Desktop" / name


class MusicDownloader:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Descargador de audio de YouTube")
        self.root.minsize(700, 500)
        self.root.iconbitmap(str(Path(__file__).with_name("MusicDownloaderLogo.ico")))

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.cancel_event = threading.Event()
        self.url = tk.StringVar()
        self.folder_name = tk.StringVar()
        self.selected_destination: Path | None = None
        self._setting_selected_folder = False
        self.tracks: list[dict[str, object]] = []
        self.destination: Path | None = None
        self.status = tk.StringVar(
            value="Primero escribe el nombre de la carpeta y pega un enlace."
        )

        self._build_ui()
        self.url.trace_add("write", self._url_changed)
        self.folder_name.trace_add("write", self._folder_name_changed)
        self.root.after(100, self._process_events)

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=16)
        main.grid(sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(3, weight=1)

        ttk.Label(main, text="Carpeta de destino:").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        self.folder_entry = ttk.Entry(main, textvariable=self.folder_name)
        self.folder_entry.grid(
            row=0, column=1, sticky="ew", pady=(0, 8)
        )
        self.browse_button = ttk.Button(
            main,
            text="📁",
            command=self._browse_destination,
            width=4,
        )
        self.browse_button.grid(row=0, column=2, sticky="ew", padx=(6, 0), pady=(0, 8))

        ttk.Label(main, text="Enlace de canción o playlist:").grid(
            row=1, column=0, sticky="w", pady=(0, 8)
        )
        self.url_entry = ttk.Entry(main, textvariable=self.url)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        actions = ttk.Frame(main)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 10))
        for column in range(3):
            actions.columnconfigure(column, weight=1)
        self.detect_button = ttk.Button(
            actions, text="Detectar canciones", command=self._start_detection
        )
        self.detect_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.download_button = ttk.Button(
            actions,
            text="Descargar canciones",
            command=self._start_download,
            state="disabled",
        )
        self.download_button.grid(row=0, column=1, sticky="ew", padx=4)
        self.cancel_button = ttk.Button(
            actions,
            text="Cancelar descarga",
            command=self._cancel_download,
            state="disabled",
        )
        self.cancel_button.grid(row=0, column=2, sticky="ew", padx=(4, 0))

        list_frame = ttk.Frame(main)
        list_frame.grid(row=3, column=0, columnspan=3, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.track_list = ttk.Treeview(
            list_frame,
            columns=("number", "title", "artist", "status"),
            show="headings",
        )
        self.track_list.heading("number", text="#")
        self.track_list.heading("title", text="Canción")
        self.track_list.heading("artist", text="Artista / canal")
        self.track_list.heading("status", text="Estado")
        self.track_list.column("number", width=48, stretch=False, anchor="center")
        self.track_list.column("title", width=300, minwidth=160)
        self.track_list.column("artist", width=200, minwidth=120)
        self.track_list.column("status", width=190, minwidth=120)
        scrollbar = ttk.Scrollbar(
            list_frame, orient="vertical", command=self.track_list.yview
        )
        self.track_list.configure(yscrollcommand=scrollbar.set)
        self.track_list.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        ttk.Label(main, textvariable=self.status, wraplength=650).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0)
        )
        ttk.Label(
            main,
            text="Se guarda solo el audio en MP3 (requiere FFmpeg). Las pistas no disponibles "
            "se omiten.",
            wraplength=650,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.folder_entry.focus_set()

    def _browse_destination(self) -> None:
        initial_dir = (
            str(self.selected_destination)
            if self.selected_destination and self.selected_destination.is_dir()
            else str(Path.home() / "Desktop")
        )
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta de destino",
            initialdir=initial_dir,
            mustexist=True,
        )
        if selected:
            destination = Path(selected)
            self.selected_destination = destination
            self._setting_selected_folder = True
            self.folder_name.set(str(destination))
            self._setting_selected_folder = False
            self.destination = destination
            self.status.set(
                f"Carpeta seleccionada: {destination}. Se comprobarán ahí las canciones existentes."
            )

    def _folder_name_changed(self, *_: str) -> None:
        if not self._setting_selected_folder:
            self.selected_destination = None

    def _prepare_destination(self) -> Path | None:
        if self.selected_destination is not None:
            if self.selected_destination.is_dir():
                return self.selected_destination
            self.selected_destination = None
        try:
            destination = desktop_folder_path(self.folder_name.get())
        except ValueError as error:
            messagebox.showerror("Nombre no válido", str(error))
            return None
        try:
            destination.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            messagebox.showerror(
                "Carpeta no disponible",
                f"No se pudo crear la carpeta en el Escritorio:\n{error}",
            )
            return None
        return destination

    def _start_detection(self) -> None:
        link = self.url.get().strip()
        if not is_youtube_url(link):
            messagebox.showerror("Enlace no válido", "Introduce un enlace válido de YouTube.")
            return
        if yt_dlp is None:
            messagebox.showerror(
                "Falta una dependencia",
                "Instala las dependencias con:\npython -m pip install -r requirements.txt",
            )
            return
        if not find_javascript_runtimes():
            messagebox.showerror(
                "Falta un runtime de JavaScript",
                "Para detectar y descargar canciones, instala Node.js LTS o Deno. "
                "Luego cierra y vuelve a abrir esta aplicación.",
            )
            return
        destination = self._prepare_destination()
        if destination is None:
            return

        self.tracks.clear()
        self.destination = destination
        self._clear_track_list()
        self.download_button.configure(state="disabled")
        self.detect_button.configure(state="disabled")
        self.folder_entry.configure(state="disabled")
        self.url_entry.configure(state="disabled")
        self.status.set("Analizando el enlace y detectando canciones…")
        threading.Thread(
            target=self._analyze,
            args=(link, destination),
            daemon=True,
        ).start()

    def _clear_track_list(self) -> None:
        for item in self.track_list.get_children():
            self.track_list.delete(item)

    def _url_changed(self, *_: str) -> None:
        if self.tracks:
            self.tracks.clear()
            self._clear_track_list()
            self.download_button.configure(state="disabled")
            self.status.set("El enlace cambió. Detecta de nuevo las canciones.")

    def _analyze(self, link: str, destination: Path) -> None:
        if yt_dlp is None:
            self.events.put(
                ("analysis_error", "Instala las dependencias con: python -m pip install -r requirements.txt")
            )
            return

        options: dict[str, object] = {
            "quiet": True,
            "color": "no_color",
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": not is_playlist_url(link),
            "ignoreerrors": True,
            "js_runtimes": find_javascript_runtimes(),
        }
        try:
            existing_ids = downloaded_video_ids(destination)
            with yt_dlp.YoutubeDL(options) as downloader:
                result = downloader.extract_info(link, download=False)
            if not result:
                raise yt_dlp.utils.DownloadError("No se pudo obtener información del enlace.")

            raw_entries = result.get("entries")
            entries = list(raw_entries) if raw_entries is not None else [result]
            tracks: list[dict[str, object]] = []
            seen_ids: set[str] = set()
            for entry in entries:
                if not entry:
                    tracks.append(
                        {
                            "title": "Canción no disponible",
                            "artist": "Desconocido",
                            "url": "",
                            "status": "Canción no disponible",
                        }
                    )
                    continue

                video_id = entry.get("id")
                video_url = entry.get("webpage_url") or entry.get("url")
                if not video_url and video_id:
                    video_url = f"https://www.youtube.com/watch?v={video_id}"
                elif video_url and not str(video_url).startswith(("http://", "https://")):
                    video_url = f"https://www.youtube.com/watch?v={video_url}"
                track_data = {"url": video_url or "", "id": video_id}
                track_id = video_id_from_track(track_data)
                already_downloaded = bool(
                    track_id
                    and (track_id in existing_ids or track_id in seen_ids)
                )
                if track_id:
                    seen_ids.add(track_id)
                tracks.append(
                    {
                        "id": track_id,
                        "title": entry.get("title") or f"Canción {len(tracks) + 1}",
                        "artist": (
                            entry.get("artist")
                            or entry.get("uploader")
                            or entry.get("channel")
                            or entry.get("creator")
                            or "Desconocido"
                        ),
                        "url": video_url or "",
                        "status": "Ya descargadas" if already_downloaded else (
                            "Lista para descargar" if video_url else "Canción no disponible"
                        ),
                    }
                )

            if not tracks:
                raise yt_dlp.utils.DownloadError("El enlace no contiene canciones.")
            self.events.put(("tracks_detected", tracks))
        except yt_dlp.utils.DownloadError:
            self.events.put(("analysis_error", "Canción no disponible"))
        except OSError:
            self.events.put(("analysis_error", "Canción no disponible"))

    def _start_download(self) -> None:
        destination = self.destination
        if not self.tracks or destination is None:
            return
        if imageio_ffmpeg is None:
            messagebox.showerror(
                "Falta FFmpeg",
                "Instala las dependencias con:\npython -m pip install -r requirements.txt",
            )
            return
        js_runtimes = find_javascript_runtimes()
        if not js_runtimes:
            messagebox.showerror(
                "Falta un runtime de JavaScript",
                "Instala Node.js LTS o Deno y vuelve a abrir esta aplicación.",
            )
            return

        try:
            ffmpeg_location = imageio_ffmpeg.get_ffmpeg_exe()
        except (RuntimeError, OSError) as error:
            messagebox.showerror("FFmpeg no disponible", str(error))
            return

        self.download_button.configure(state="disabled")
        self.detect_button.configure(state="disabled")
        self.cancel_event.clear()
        self.cancel_button.configure(state="normal")
        self.folder_entry.configure(state="disabled")
        self.url_entry.configure(state="disabled")
        self.status.set(
            f"Descargando {len(self.tracks)} canciones en «{destination.name}»…"
        )
        threading.Thread(
            target=self._download_tracks,
            args=(list(self.tracks), destination, ffmpeg_location, js_runtimes),
            daemon=True,
        ).start()

    def _cancel_download(self) -> None:
        if not self.cancel_event.is_set():
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.status.set("Cancelando la pista actual…")

    def _download_tracks(
        self,
        tracks: list[dict[str, object]],
        destination: Path,
        ffmpeg_location: str,
        js_runtimes: dict[str, dict[str, str]],
    ) -> None:
        if yt_dlp is None:
            self.events.put(("error", "Instala las dependencias con: python -m pip install -r requirements.txt"))
            return

        succeeded = 0
        failed = 0
        omitted = 0
        cancelled = False
        try:
            existing_ids = downloaded_video_ids(destination)
        except OSError as error:
            self.events.put(("error", str(error)))
            return

        seen_ids: set[str] = set()
        tracks_to_download: list[tuple[int, dict[str, object]]] = []
        for index, track in enumerate(tracks, start=1):
            if track.get("status") == "Canción no disponible" or not track.get("url"):
                omitted += 1
                self.events.put(("track_update", (index, "Canción no disponible")))
                continue

            video_id = video_id_from_track(track)
            if (
                track.get("status") == "Ya descargadas"
                or (video_id and (video_id in existing_ids or video_id in seen_ids))
            ):
                omitted += 1
                self.events.put(("track_update", (index, "Anteriormente descargada")))
                continue
            if video_id:
                seen_ids.add(video_id)
            tracks_to_download.append((index, track))

        track_iterator = iter(tracks_to_download)
        pending: dict[Future[str], int] = {}

        with ThreadPoolExecutor(max_workers=3) as executor:
            while len(pending) < 3 and not self.cancel_event.is_set():
                try:
                    index, track = next(track_iterator)
                except StopIteration:
                    break
                future = executor.submit(
                    self._download_one_track,
                    index,
                    track,
                    destination,
                    ffmpeg_location,
                    js_runtimes,
                )
                pending[future] = index

            while pending:
                completed, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    pending.pop(future)
                    outcome = future.result()
                    if outcome == "success":
                        succeeded += 1
                    elif outcome == "omitted":
                        omitted += 1
                    elif outcome == "failed":
                        failed += 1
                    elif outcome == "cancelled":
                        cancelled = True
                    elif outcome == "not_started":
                        cancelled = True

                if self.cancel_event.is_set():
                    cancelled = True
                while (
                    len(pending) < 3
                    and not cancelled
                    and not self.cancel_event.is_set()
                ):
                    try:
                        index, track = next(track_iterator)
                    except StopIteration:
                        break
                    future = executor.submit(
                        self._download_one_track,
                        index,
                        track,
                        destination,
                        ffmpeg_location,
                        js_runtimes,
                    )
                    pending[future] = index

        if cancelled:
            for pending_index, _ in track_iterator:
                self.events.put(("track_update", (pending_index, "No iniciada")))
        self.events.put(("download_finished", (succeeded, failed, omitted, cancelled)))

    def _download_one_track(
        self,
        index: int,
        track: dict[str, object],
        destination: Path,
        ffmpeg_location: str,
        js_runtimes: dict[str, dict[str, str]],
    ) -> str:
        if self.cancel_event.is_set():
            self.events.put(("track_update", (index, "No iniciada")))
            return "not_started"

        link = track.get("url")
        if not link:
            self.events.put(("track_update", (index, "Canción no disponible")))
            return "omitted"

        video_id = video_id_from_track(track) or "%(id)s"
        self.events.put(("track_update", (index, "Iniciando…")))
        options: dict[str, object] = {
            "format": "bestaudio/best",
            "outtmpl": str(
                destination
                / f"%(title)s - %(uploader)s [{video_id}].%(ext)s"
            ),
            "noplaylist": True,
            "nooverwrites": True,
            "quiet": True,
            "color": "no_color",
            "ffmpeg_location": ffmpeg_location,
            "js_runtimes": js_runtimes,
            "progress_hooks": [
                lambda data: self._track_progress_hook(index, data)
            ],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        try:
            with yt_dlp.YoutubeDL(options) as downloader:
                downloader.extract_info(str(link), download=True)
            self.events.put(("track_update", (index, "Descargada")))
            return "success"
        except yt_dlp.utils.DownloadCancelled:
            self.events.put(("track_update", (index, "Cancelada")))
            return "cancelled"
        except yt_dlp.utils.DownloadError:
            if self.cancel_event.is_set():
                self.events.put(("track_update", (index, "Cancelada")))
                return "cancelled"
            self.events.put(("track_update", (index, "Canción no disponible")))
            return "failed"
        except OSError:
            if self.cancel_event.is_set():
                self.events.put(("track_update", (index, "Cancelada")))
                return "cancelled"
            self.events.put(("track_update", (index, "Canción no disponible")))
            return "failed"

    def _track_progress_hook(self, index: int, data: dict[str, object]) -> None:
        if self.cancel_event.is_set():
            if yt_dlp is not None:
                raise yt_dlp.utils.DownloadCancelled()
            return

        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            if isinstance(total, (int, float)) and total > 0 and isinstance(downloaded, (int, float)):
                status = f"Descargando… {downloaded / total * 100:.0f}%"
            else:
                status = "Descargando…"
            self.events.put(("track_update", (index, status)))
        elif data.get("status") == "finished":
            self.events.put(("track_update", (index, "Convirtiendo a MP3…")))

    def _process_events(self) -> None:
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "tracks_detected":
                    if not isinstance(value, list):
                        continue
                    self.tracks = value
                    self._clear_track_list()
                    ready_count = 0
                    for index, track in enumerate(self.tracks, start=1):
                        title = str(track["title"])
                        track_status = str(track["status"])
                        self.track_list.insert(
                            "",
                            "end",
                            iid=str(index),
                            values=(
                                index,
                                title,
                                str(track.get("artist", "Desconocido")),
                                track_status,
                            ),
                        )
                        if track.get("url") and track_status == "Lista para descargar":
                            ready_count += 1
                    self.detect_button.configure(state="normal")
                    self.folder_entry.configure(state="normal")
                    self.url_entry.configure(state="normal")
                    self.download_button.configure(
                        state="normal" if ready_count else "disabled"
                    )
                    self.status.set(
                        f"Se detectaron {len(self.tracks)} canciones; "
                        f"{ready_count} disponibles para descargar."
                    )
                elif event == "analysis_error":
                    self.detect_button.configure(state="normal")
                    self.folder_entry.configure(state="normal")
                    self.url_entry.configure(state="normal")
                    self.download_button.configure(state="disabled")
                    self.status.set("Canción no disponible")
                elif event == "track_update":
                    if not isinstance(value, tuple) or len(value) != 2:
                        continue
                    index, track_status = value
                    row = str(index)
                    if self.track_list.exists(row):
                        values = list(self.track_list.item(row, "values"))
                        values[3] = track_status
                        self.track_list.item(row, values=values)
                    self.status.set(f"Canción {index}: {track_status}")
                elif event == "download_finished":
                    if not isinstance(value, tuple) or len(value) != 4:
                        continue
                    succeeded, failed, omitted, cancelled = value
                    self.detect_button.configure(state="normal")
                    self.folder_entry.configure(state="normal")
                    self.url_entry.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    can_retry = any(track.get("url") for track in self.tracks)
                    self.download_button.configure(
                        state="normal" if can_retry else "disabled"
                    )
                    if cancelled:
                        self.status.set(
                            f"Descarga cancelada: {succeeded} completadas; "
                            f"{failed} con error; {omitted} omitidas."
                        )
                    elif failed or omitted:
                        self.status.set(
                            f"Proceso terminado: {succeeded} descargadas; "
                            f"{failed} con error; {omitted} omitidas."
                        )
                    else:
                        self.status.set(f"Proceso terminado: {succeeded} canciones descargadas.")
                elif event == "error":
                    self.status.set("La descarga no se pudo completar.")
                    self.detect_button.configure(state="normal")
                    self.folder_entry.configure(state="normal")
                    self.url_entry.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.download_button.configure(state="normal" if self.tracks else "disabled")
                    messagebox.showerror("Error de descarga", str(value))
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)


def main() -> None:
    if sys.platform == "win32":
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        set_app_id = shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        result = set_app_id("MusicDownloader.YouTubeAudio")
        if result < 0:
            raise OSError(result, "No se pudo configurar la identidad de la aplicación.")

    root = tk.Tk()
    MusicDownloader(root)
    root.mainloop()


if __name__ == "__main__":
    main()