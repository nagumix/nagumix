# src/settings_manager.py
import os
import configparser
import copy
import tempfile


ARRANGEMENT_SECTION = "Arrangement"
ARRANGEMENT_DEFAULTS = {"spacing": 10, "outer_margin": 10}
ARRANGEMENT_MIN_PX = 0
ARRANGEMENT_MAX_PX = 1000
OVERLAY_TIMEOUT_DEFAULT_MS = 1500
OVERLAY_TIMEOUT_MIN_MS = 100
OVERLAY_TIMEOUT_MAX_MS = 10000
PRELOAD_CACHE_DEFAULT_MB = 256
PRELOAD_CACHE_MIN_MB = 0
PRELOAD_CACHE_MAX_MB = 4096
SORT_METHOD_DEFAULT = "natural_asc"
SORT_METHODS = (
    "natural_asc", "natural_desc",
    "name_asc", "name_desc",
    "date_asc", "date_desc",
    "size_asc", "size_desc",
)
OBJECT_INFO_POSITION_DEFAULT = "top_center"
OBJECT_INFO_POSITIONS = (
    "top_left", "top_center", "top_right",
    "middle_left", "center", "middle_right",
    "bottom_left", "bottom_center", "bottom_right",
)
APPEARANCE_MODES = ("light", "dark", "system")
APPEARANCE_DEFAULT = "system"
FILE_IDENTIFICATION_DEFAULT = True


class SettingsManager:
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.loaded_path = None
        self.default_file_name = "nagumix_settings.ini"

        # Attempt to load from local folder first, else from user config folder
        if os.path.exists(self.default_file_name):
            self.loaded_path = self.default_file_name
        else:
            # E.g., use an OS-specific path. For simplicity, let's just try local only:
            pass

        if self.loaded_path:
            self.config.read(self.loaded_path)
        else:
            # We have no existing .ini; use defaults
            pass

        # Ensure sections exist
        if not self.config.has_section("Canvas"):
            self.config.add_section("Canvas")
            self.config.set("Canvas", "background_color", "#FFFFFF")

        # Navigation settings
        if not self.config.has_section("Navigation"):
            self.config.add_section("Navigation")
            self.config.set("Navigation", "preload_count", "1")  # Number of images to preload in each direction
            self.config.set("Navigation", "enable_wheel_navigation", "true")
        if not self.config.has_option("Navigation", "sort_method"):
            self.config.set("Navigation", "sort_method", SORT_METHOD_DEFAULT)
        if not self.config.has_option("Navigation", "preload_cache_mb"):
            self.config.set(
                "Navigation", "preload_cache_mb", str(PRELOAD_CACHE_DEFAULT_MB))

        # UI settings
        if not self.config.has_section("UI"):
            self.config.add_section("UI")
            self.config.set("UI", "overlay_timeout_ms", str(OVERLAY_TIMEOUT_DEFAULT_MS))
        if not self.config.has_option("UI", "object_info_position"):
            self.config.set("UI", "object_info_position", OBJECT_INFO_POSITION_DEFAULT)

        self._load_arrangement_settings()

    @staticmethod
    def _validated_arrangement_value(value):
        if type(value) is int:
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = int(value.strip())
            except ValueError:
                return None
        else:
            return None
        if ARRANGEMENT_MIN_PX <= parsed <= ARRANGEMENT_MAX_PX:
            return parsed
        return None

    def _load_arrangement_settings(self):
        """Install validated defaults for absent, partial, or malformed INI data."""
        if not self.config.has_section(ARRANGEMENT_SECTION):
            self.config.add_section(ARRANGEMENT_SECTION)
        for key, default in ARRANGEMENT_DEFAULTS.items():
            stored = self.config.get(ARRANGEMENT_SECTION, key, fallback=None)
            value = self._validated_arrangement_value(stored)
            self.config.set(
                ARRANGEMENT_SECTION, key, str(default if value is None else value))

    def get_arrangement_settings(self):
        return {
            key: int(self.config.get(ARRANGEMENT_SECTION, key))
            for key in ARRANGEMENT_DEFAULTS
        }

    def set_arrangement_settings(self, spacing, outer_margin):
        """Validate and apply both arrangement preferences atomically."""
        values = {
            "spacing": self._validated_arrangement_value(spacing),
            "outer_margin": self._validated_arrangement_value(outer_margin),
        }
        if any(value is None for value in values.values()):
            raise ValueError(
                f"Arrangement values must be whole pixels from "
                f"{ARRANGEMENT_MIN_PX} to {ARRANGEMENT_MAX_PX}.")
        for key, value in values.items():
            self.config.set(ARRANGEMENT_SECTION, key, str(value))

    def get_setting(self, section, key, fallback=None):
        if self.config.has_option(section, key):
            return self.config.get(section, key)
        return fallback

    def get_sort_method(self):
        """Return a persisted navigation sort value or the natural default."""
        value = self.get_setting("Navigation", "sort_method", SORT_METHOD_DEFAULT)
        value = str(value).strip().lower()
        return value if value in SORT_METHODS else SORT_METHOD_DEFAULT

    def set_sort_method(self, value):
        """Validate and store one of the supported navigation sort values."""
        value = str(value).strip().lower()
        if value not in SORT_METHODS:
            raise ValueError(f"Unknown navigation sort method: {value}")
        self.set_setting("Navigation", "sort_method", value)

    def get_overlay_timeout_ms(self):
        """Return the validated status timeout, repairing no stored data."""
        raw = self.get_setting(
            "UI", "overlay_timeout_ms", str(OVERLAY_TIMEOUT_DEFAULT_MS))
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return OVERLAY_TIMEOUT_DEFAULT_MS
        if not OVERLAY_TIMEOUT_MIN_MS <= value <= OVERLAY_TIMEOUT_MAX_MS:
            return OVERLAY_TIMEOUT_DEFAULT_MS
        return value

    def get_object_info_position(self):
        """Return a validated object status/info overlay placement."""
        value = str(self.get_setting(
            "UI", "object_info_position", OBJECT_INFO_POSITION_DEFAULT)).strip().lower()
        return value if value in OBJECT_INFO_POSITIONS else OBJECT_INFO_POSITION_DEFAULT

    def set_object_info_position(self, value):
        """Validate and store an object status/info overlay placement."""
        value = str(value).strip().lower()
        if value not in OBJECT_INFO_POSITIONS:
            raise ValueError(f"Unknown object info position: {value}")
        self.set_setting("UI", "object_info_position", value)

    def get_preload_cache_mb(self):
        """Return the validated decoded-preload payload budget in MiB."""
        raw = self.get_setting(
            "Navigation", "preload_cache_mb", str(PRELOAD_CACHE_DEFAULT_MB))
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return PRELOAD_CACHE_DEFAULT_MB
        if not PRELOAD_CACHE_MIN_MB <= value <= PRELOAD_CACHE_MAX_MB:
            return PRELOAD_CACHE_DEFAULT_MB
        return value

    def set_setting(self, section, key, value):
        if not self.config.has_section(section):
            self.config.add_section(section)
        self.config.set(section, key, str(value))

    def get_appearance_mode(self):
        """Return a stable preference without repairing/writing the INI."""
        value = str(self.get_setting("UI", "appearance_mode", APPEARANCE_DEFAULT)).strip().lower()
        return value if value in APPEARANCE_MODES else APPEARANCE_DEFAULT

    def set_appearance_mode(self, value):
        value = str(value).strip().lower()
        if value not in APPEARANCE_MODES:
            raise ValueError(f"Unknown appearance mode: {value}")
        self.set_setting("UI", "appearance_mode", value)

    def get_include_file_identification(self):
        """Return the safe default for missing or malformed saving metadata."""
        raw = self.get_setting(
            "Saving", "include_file_identification",
            str(FILE_IDENTIFICATION_DEFAULT).lower())
        normalized = str(raw).strip().lower()
        if normalized in ("1", "yes", "true", "on"):
            return True
        if normalized in ("0", "no", "false", "off"):
            return False
        return FILE_IDENTIFICATION_DEFAULT

    def get_dialog_draft(self):
        """Snapshot every page before lazy construction; no defaults on Save."""
        try:
            preload = int(self.get_setting("Navigation", "preload_count", "2"))
        except (TypeError, ValueError):
            preload = 2
        arrangement = self.get_arrangement_settings()
        return dict(
            background=self.get_setting("Canvas", "background_color", "#FFFFFF"),
            mode=self.get_appearance_mode(), timeout=self.get_overlay_timeout_ms(),
            position=self.get_object_info_position(),
            wheel=self.get_setting("Navigation", "enable_wheel_navigation", "true").lower() == "true",
            sort=self.get_sort_method(), preload=max(0, min(5, preload)),
            spacing=arrangement["spacing"], margin=arrangement["outer_margin"],
            include_file_identification=self.get_include_file_identification(),
        )

    def application_snapshot(self):
        return {**self.get_dialog_draft(), "cache_mb": self.get_preload_cache_mb()}

    def save_dialog_draft(self, draft):
        """Validate completely, write atomically, then publish accepted values."""
        for key, low, high in (("timeout", 100, 10000), ("preload", 0, 5),
                               ("spacing", 0, 1000), ("margin", 0, 1000)):
            if type(draft[key]) is not int or not low <= draft[key] <= high:
                raise ValueError(f"{key.capitalize()} must be a whole number from {low} to {high}.")
        for key, values in (("mode", APPEARANCE_MODES), ("sort", SORT_METHODS),
                            ("position", OBJECT_INFO_POSITIONS)):
            if draft[key] not in values:
                raise ValueError(f"Invalid {key}: {draft[key]}")
        if (type(draft["wheel"]) is not bool
                or type(draft["include_file_identification"]) is not bool
                or not isinstance(draft["background"], str)):
            raise ValueError("Invalid background, mouse wheel or file identification setting.")
        # Keep legacy free-text color semantics; color validation remains U03.
        candidate = copy.deepcopy(self.config)
        values = {
            "Canvas": {"background_color": draft["background"].strip()},
            "UI": {"appearance_mode": draft["mode"], "overlay_timeout_ms": draft["timeout"],
                   "object_info_position": draft["position"]},
            "Navigation": {"sort_method": draft["sort"], "preload_count": draft["preload"],
                           "enable_wheel_navigation": str(draft["wheel"]).lower()},
            "Arrangement": {"spacing": draft["spacing"], "outer_margin": draft["margin"]},
            "Saving": {"include_file_identification":
                       str(draft["include_file_identification"]).lower()},
        }
        for section, settings in values.items():
            if not candidate.has_section(section):
                candidate.add_section(section)
            for key, value in settings.items():
                candidate.set(section, key, str(value))
        path = self._write_config(candidate)
        self.config = candidate
        self.loaded_path = path

    def _write_config(self, config):
        path = self.loaded_path or self.default_file_name
        directory = os.path.dirname(os.path.abspath(path))
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".nagumix-settings-", suffix=".tmp",
                                             delete=False) as stream:
                temporary = stream.name
                config.write(stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        return path

    def save(self):
        """Atomically save current settings without relocating the INI."""
        self.loaded_path = self._write_config(self.config)
