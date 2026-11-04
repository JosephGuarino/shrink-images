# shrink_images

A command-line tool that shrinks images in a directory to a target file size by reducing their resolution.

- Only images larger than the target size are modified.
- Aspect ratio is always preserved.
- Images already at or under the target size are left untouched.

## Requirements

- Python 3.10+
- [Pillow](https://pypi.org/project/Pillow/)

## Usage

```
python shrink_images.py <directory> --size <target size> [options]
```

### Options

| Option | Description |
| --- | --- |
| `-s`, `--size` | **Required.** Target max file size. Accepts `B`, `KB`, `MB`, `GB` (e.g. `500KB`, `1MB`, `2.5MB`). |
| `-n`, `--dry-run` | Show what would change without modifying any files. |
| `-b`, `--backup-dir DIR` | Copy originals into `DIR` (folder structure preserved) before overwriting. |
| `-j`, `--jobs N` | Number of images to process in parallel. Defaults to your CPU count. |
| `--no-recursive` | Only process the top-level directory, not subdirectories. |
| `--min-scale` | Smallest allowed scale factor. Default is `0.05` (5% of original dimensions). |

### Examples

Shrink every image over 1 MB in `~/Photos` (including subfolders):

```
python shrink_images.py ~/Photos --size 1MB
```

Keep a backup of the originals:

```
python shrink_images.py ./images --size 2MB --backup-dir ./originals
```

Only process the top-level folder:

```
python shrink_images.py ./images --size 1MB --no-recursive
```

## Supported formats

JPEG, PNG, WebP, BMP, and TIFF. Each file keeps its original format. Animated images are skipped.
