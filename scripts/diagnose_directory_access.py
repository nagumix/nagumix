"""Compare read-only image access and parent-directory enumeration.

The diagnostic intentionally prints operation results and error codes without
printing the supplied server/share/path. It does not retry, map drives, request
credentials, or change Windows/SMB settings.
"""

import argparse
import os
import stat


def error_details(exc):
    code = getattr(exc, "winerror", None)
    if code is None:
        code = getattr(exc, "errno", None)
    return f"FAILED code={code!r} type={type(exc).__name__} message={exc.strerror or str(exc)!r}"


def run(image_path, show_entries=False):
    parent = os.path.dirname(image_path)
    path_form = "extended-UNC" if image_path.lower().startswith("\\\\?\\unc\\") else "ordinary"
    print(f"path-form={path_form}")
    print(f"parent-present={bool(parent)}")

    try:
        exists = os.path.exists(image_path)
        print(f"exists=OK value={exists}")
    except OSError as exc:
        print(f"exists={error_details(exc)}")

    try:
        with open(image_path, "rb") as stream:
            stream.read(1)
        print("open-read=OK")
    except OSError as exc:
        print(f"open-read={error_details(exc)}")

    try:
        names = os.listdir(parent)
        print(f"listdir=OK entries={len(names)}")
    except OSError as exc:
        print(f"listdir={error_details(exc)}")
        return

    candidates = 0
    image_entries = 0
    non_files = 0
    stat_failures = []
    supported = {
        '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tif', '.tiff', '.tga'
    }
    for name in names:
        extension = os.path.splitext(name)[1].lower()
        if extension not in supported:
            if show_entries:
                print(f"entry={name!r} extension={extension!r} classification=unsupported")
            continue
        candidates += 1
        full_path = os.path.join(parent, name)
        try:
            metadata = os.stat(full_path)
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            if code is None:
                code = getattr(exc, "errno", None)
            stat_failures.append(code)
            if show_entries:
                print(f"entry={name!r} extension={extension!r} classification=stat-failed code={code!r}")
            continue
        if stat.S_ISREG(metadata.st_mode):
            image_entries += 1
            classification = "file"
        else:
            non_files += 1
            classification = "not-file"
        if show_entries:
            attributes = getattr(metadata, "st_file_attributes", None)
            reparse_tag = getattr(metadata, "st_reparse_tag", None)
            print(f"entry={name!r} extension={extension!r} "
                  f"classification={classification} mode={metadata.st_mode:#o} "
                  f"attributes={attributes!r} reparse-tag={reparse_tag!r}")
    print("image-metadata=OK "
          f"candidates={candidates} usable={image_entries} "
          f"not-files={non_files} failures={stat_failures}")

    try:
        with os.scandir(parent) as iterator:
            directory_entries = list(iterator)
    except OSError as exc:
        print(f"scandir={error_details(exc)}")
        return

    scan_candidates = 0
    scan_usable = 0
    scan_failures = []
    for entry in directory_entries:
        extension = os.path.splitext(entry.name)[1].lower()
        if extension not in supported:
            continue
        scan_candidates += 1
        try:
            is_file = entry.is_file()
            entry.stat()
        except OSError as exc:
            code = getattr(exc, "winerror", None)
            if code is None:
                code = getattr(exc, "errno", None)
            scan_failures.append(code)
            if show_entries:
                print(f"scandir-entry={entry.name!r} classification=failed code={code!r}")
            continue
        if is_file:
            scan_usable += 1
        if show_entries:
            classification = "file" if is_file else "not-file"
            attributes = getattr(entry.stat(), "st_file_attributes", None)
            print(f"scandir-entry={entry.name!r} classification={classification} "
                  f"attributes={attributes!r}")
    print("scandir=OK "
          f"candidates={scan_candidates} usable={scan_usable} "
          f"failures={scan_failures}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Compare image opening with parent-directory enumeration")
    parser.add_argument("image_path", help="Affected image path; never printed")
    parser.add_argument(
        "--show-entries", action="store_true",
        help="Print entry names, extensions, and metadata classification")
    arguments = parser.parse_args()
    run(arguments.image_path, arguments.show_entries)
