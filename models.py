from dataclasses import dataclass

@dataclass
class FileInfo:
    path: str
    size: int
    created: float
    is_adb: bool = False

@dataclass
class Settings:
    scan_root: str
    output_root: str
    criteria: str
    hash_algo: str
    hash_mode: str
    only_media: bool
    extensions: list
    min_size_kb: int
    exclude_dirs: list
    skip_hidden_system: bool
    dry_run: bool
    preserve_structure: bool
    max_hash_workers: int
    use_adb: bool = False
    adb_serial: str = ""
    log_folder: str = ""       
    isolate_folder: str = ""   

@dataclass
class TransferSettings:
    source_root: str
    dest_root: str
    output_root: str
    criteria: str
    hash_algo: str
    hash_mode: str
    only_media: bool
    extensions: list
    min_size_kb: int
    exclude_dirs: list
    skip_hidden_system: bool
    dry_run: bool
    preserve_structure: bool
    max_hash_workers: int
    transfer_mode: str
    duplicate_policy: str      
    use_dest_cache: bool
    source_is_adb: bool = False
    adb_serial: str = ""
    log_folder: str = ""       
    isolate_folder: str = ""
    drive_cache_path: str = ""
    update_drive_cache: bool = True
    use_adb_cache: bool = True
    adb_cache_path: str = ""
    transfer_profile: str = "Balanced"
    retry_attempts: int = 3
    conflict_policy: str = "rename"
    keep_device_awake: bool = True
    journal_path: str = ""
    reconnect_timeout: int = 300
    stall_timeout: int = 180
    destination_template: str = "preserve"
