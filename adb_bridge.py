import subprocess
import os
import time
from models import FileInfo

CREATE_NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
ADB_QUICK_TIMEOUT = 3
ADB_PROBE_TIMEOUT = 8


class ADBOperationError(RuntimeError):
    def __init__(self, operation, path, detail):
        self.operation = operation
        self.path = path
        self.detail = detail.strip() or "ADB command failed."
        lowered = self.detail.lower()
        self.device_unavailable = any(
            marker in lowered
            for marker in ("unauthorized", "offline", "no devices", "device not found", "device disconnected")
        )
        super().__init__(f"{operation} failed for {path}: {self.detail}")

class ADBBridge:
    @staticmethod
    def start_server():
        try:
            subprocess.run(
                ADBBridge._adb_command("start-server"),
                check=False,
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_QUICK_TIMEOUT,
            )
        except Exception:
            pass

    @staticmethod
    def _adb_command(*args, serial=None):
        cmd = ["adb"]
        if serial:
            cmd.extend(["-s", serial])
        cmd.extend(args)
        return cmd

    @staticmethod
    def _shell_quote(value):
        return "'" + str(value).replace("'", "'\"'\"'") + "'"

    @staticmethod
    def normalize_remote_path(remote_path):
        path = (remote_path or "").strip().replace("\\", "/")
        if not path:
            return path
        if not path.startswith("/"):
            path = f"/{path}"
        if path == "/sd":
            path = "/sdcard"
        elif path.startswith("/sd/"):
            path = f"/sdcard/{path[4:]}"
        elif path.startswith("/sdcard/"):
            path = path
        elif path == "/storage/self/primary":
            path = "/sdcard"
        elif path.startswith("/storage/self/primary/"):
            path = f"/sdcard/{path[len('/storage/self/primary/'):]}"
        elif path == "/storage/emulated/0":
            path = "/sdcard"
        elif path.startswith("/storage/emulated/0/"):
            path = f"/sdcard/{path[len('/storage/emulated/0/'):]}"

        parts = path.split("/")
        if len(parts) > 2 and parts[1] == "sdcard":
            common_dirs = {
                "dcim": "DCIM",
                "pictures": "Pictures",
                "picture": "Pictures",
                "download": "Download",
                "downloads": "Download",
                "movies": "Movies",
                "videos": "Movies",
                "music": "Music",
                "audio": "Music",
            }
            parts[2] = common_dirs.get(parts[2].lower(), parts[2])
            path = "/".join(parts)
        return path.rstrip("/") or "/"

    @staticmethod
    def remote_path_status(remote_path, serial=None):
        normalized = ADBBridge.normalize_remote_path(remote_path)
        quoted_path = ADBBridge._shell_quote(normalized)
        script = (
            f"if [ -d {quoted_path} ]; then echo dir; "
            f"elif [ -e {quoted_path} ]; then echo file; "
            "else echo missing; fi"
        )
        try:
            output = subprocess.check_output(
                ADBBridge._adb_command("exec-out", "sh", "-c", script, serial=serial),
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_PROBE_TIMEOUT,
            ).decode(errors="replace").strip()
            return output.splitlines()[-1] if output else "missing"
        except Exception:
            return "error"

    @staticmethod
    def list_devices():
        devices = []
        try:
            ADBBridge.start_server()
            output = subprocess.check_output(
                ADBBridge._adb_command("devices", "-l"),
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_QUICK_TIMEOUT,
            ).decode(errors="replace").splitlines()
        except Exception:
            return devices

        for line in output[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial = parts[0]
            status = parts[1] if len(parts) > 1 else "unknown"
            model = serial
            detail_tokens = parts[2:]
            for token in detail_tokens:
                if token.startswith("model:"):
                    model = token.split(":", 1)[1].replace("_", " ")
                    break
            if status == "device" and model == serial:
                try:
                    model = subprocess.check_output(
                        ADBBridge._adb_command("shell", "getprop", "ro.product.model", serial=serial),
                        creationflags=CREATE_NO_WINDOW,
                        timeout=ADB_QUICK_TIMEOUT,
                    ).decode(errors="replace").strip() or serial
                except Exception:
                    model = serial
            devices.append({"serial": serial, "model": model, "status": status})
        return devices

    @staticmethod
    def probe_device(serial):
        if not serial:
            return False, "ADB device serial is required."
        ADBBridge.start_server()
        try:
            subprocess.check_output(
                ADBBridge._adb_command("exec-out", "sh", "-c", "echo __adb_ready__", serial=serial),
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_PROBE_TIMEOUT,
            )
            return True, ""
        except subprocess.CalledProcessError as exc:
            output = exc.output.decode(errors="replace") if getattr(exc, "output", None) else str(exc)
            return False, output.strip() or "ADB shell probe failed."
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def wait_for_device(serial, timeout=20, stop_event=None):
        deadline = time.monotonic() + timeout
        last_detail = "Timed out waiting for ADB device."
        while time.monotonic() < deadline:
            if stop_event and stop_event.is_set():
                return False, "Cancelled while waiting for the ADB device."
            ready, last_detail = ADBBridge.probe_device(serial)
            if ready:
                return True, ""
            time.sleep(2)
        return False, last_detail

    @staticmethod
    def enable_usb_stay_awake(serial):
        try:
            previous = subprocess.check_output(
                ADBBridge._adb_command(
                    "exec-out", "sh", "-c", "settings get global stay_on_while_plugged_in", serial=serial
                ),
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_PROBE_TIMEOUT,
            ).decode(errors="replace").strip()
            subprocess.run(
                ADBBridge._adb_command("shell", "svc", "power", "stayon", "usb", serial=serial),
                check=False,
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_PROBE_TIMEOUT,
            )
            return previous
        except Exception:
            return None

    @staticmethod
    def restore_stay_awake(serial, previous):
        if previous is None:
            return
        try:
            subprocess.run(
                ADBBridge._adb_command(
                    "exec-out",
                    "sh",
                    "-c",
                    f"settings put global stay_on_while_plugged_in {ADBBridge._shell_quote(previous)}",
                    serial=serial,
                ),
                check=False,
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_PROBE_TIMEOUT,
            )
        except Exception:
            pass

    @staticmethod
    def get_device_state(serial):
        if not serial:
            return ""
        for device in ADBBridge.list_devices():
            if device["serial"] == serial:
                return device["status"]
        return ""

    @staticmethod
    def get_device_info(serial=None):
        try:
            ADBBridge.start_server()
            if not serial:
                devices = ADBBridge.list_devices()
                online = next((d for d in devices if d["status"] == "device"), None)
                if not online:
                    return {"model": "None", "status": "Disconnected", "serial": ""}
                serial = online["serial"]
                model = online["model"]
            else:
                device = next((d for d in ADBBridge.list_devices() if d["serial"] == serial), None)
                if device and device["status"] != "device":
                    return {"model": device["model"], "status": device["status"], "serial": serial}
                model = subprocess.check_output(
                    ADBBridge._adb_command("shell", "getprop", "ro.product.model", serial=serial),
                    creationflags=CREATE_NO_WINDOW,
                    timeout=ADB_QUICK_TIMEOUT,
                ).decode(errors="replace").strip()
            return {"model": model or serial, "status": "Connected", "serial": serial}
        except Exception:
            return {"model": "None", "status": "Disconnected", "serial": serial or ""}

    @staticmethod
    def get_storage_info(serial=None):
        try:
            ADBBridge.start_server()
            out = subprocess.check_output(
                ADBBridge._adb_command("shell", "df /sdcard", serial=serial),
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_PROBE_TIMEOUT,
            ).decode(errors="replace").splitlines()[1].split()
            total, used = int(out[1]) * 1024, int(out[2]) * 1024
            return used, total, (used / total)
        except Exception:
            return 0, 1, 0

    @staticmethod
    def get_directory_structure(remote_path, serial=None):
        remote_path = ADBBridge.normalize_remote_path(remote_path)
        quoted_path = ADBBridge._shell_quote(remote_path)
        cmd = ADBBridge._adb_command("shell", f"ls -p {quoted_path}", serial=serial)
        try:
            output = subprocess.check_output(
                cmd,
                creationflags=CREATE_NO_WINDOW,
                timeout=ADB_PROBE_TIMEOUT,
            ).decode(errors="replace").splitlines()
            dirs = [{"name": i.strip('/'), "path": f"{remote_path.rstrip('/')}/{i.strip('/')}"} for i in output if i.endswith('/')]
            return sorted(dirs, key=lambda x: x['name'].lower())
        except Exception:
            return []

    @staticmethod
    def list_files_recursive(remote_path, settings, stop_event, serial=None):
        from discovery import scan_adb_tree

        serial = serial or getattr(settings, "adb_serial", "")
        if serial and not getattr(settings, "adb_serial", ""):
            settings.adb_serial = serial
        result = scan_adb_tree(remote_path, settings, stop_event)
        for item in result.files:
            yield item

    @staticmethod
    def remote_hash(path, algo, serial=None):
        path = ADBBridge.normalize_remote_path(path)
        quoted_path = ADBBridge._shell_quote(path)
        cmd = ADBBridge._adb_command(
            "exec-out",
            "sh",
            "-c",
            f"{'sha256sum' if algo=='sha256' else 'md5sum'} {quoted_path}",
            serial=serial,
        )
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ADBOperationError("Hash", path, detail)
        output = result.stdout.split()
        if not output:
            raise ADBOperationError("Hash", path, "The device returned no hash output.")
        return output[0]

    @staticmethod
    def pull(
        src,
        dst,
        serial=None,
        disable_compression=False,
        progress_callback=None,
        stall_timeout=180,
    ):
        src = ADBBridge.normalize_remote_path(src)
        args = ["pull"]
        if disable_compression:
            args.append("-Z")
        args.extend([src, dst])
        process = subprocess.Popen(
            ADBBridge._adb_command(*args, serial=serial),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        last_size = -1
        last_progress = time.monotonic()
        while process.poll() is None:
            current_size = 0
            try:
                current_size = os.path.getsize(dst) if os.path.exists(dst) else 0
            except OSError:
                pass
            if current_size != last_size:
                last_size = current_size
                last_progress = time.monotonic()
            elif stall_timeout and time.monotonic() - last_progress >= stall_timeout:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise ADBOperationError(
                    "Pull",
                    src,
                    f"No transfer progress for {stall_timeout} seconds; the stalled command was restarted.",
                )
            if progress_callback:
                progress_callback(current_size)
            time.sleep(0.5)
        stdout, stderr = process.communicate()
        returncode = process.returncode
        detail = stderr.strip() or stdout.strip()
        if returncode != 0 and disable_compression and (
            "unknown option" in detail.lower() or "usage:" in detail.lower()
        ):
            process = subprocess.run(
                ADBBridge._adb_command("pull", src, dst, serial=serial),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            returncode = process.returncode
            detail = process.stderr.strip() or process.stdout.strip()
        if returncode != 0:
            raise ADBOperationError("Pull", src, detail)
