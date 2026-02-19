import argparse
import re
import statistics
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageChops, ImageStat


VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
OUTPUT_MODES = ("images", "cbz", "both")


def natural_sort_key(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def is_mostly_white(pil_img: Image.Image, ratio_threshold: float = 0.98, white_cutoff: int = 245) -> bool:
    gray = pil_img.convert("L")
    histogram = gray.histogram()
    white_pixels = sum(histogram[white_cutoff + 1 :])
    ratio_white = white_pixels / (gray.width * gray.height)
    return ratio_white > ratio_threshold


def normalize_width(img: Image.Image, target_width: int) -> Image.Image:
    if img.width == target_width:
        return img
    if img.width > target_width:
        return img.crop((0, 0, target_width, img.height))
    canvas = Image.new("RGB", (target_width, img.height), (255, 255, 255))
    canvas.paste(img, (0, 0))
    return canvas


def trim_top(img: Image.Image, pixels: int) -> Image.Image:
    if pixels <= 0:
        return img
    if pixels >= img.height:
        raise ValueError(f"trim-top ({pixels}) is >= image height ({img.height}).")
    return img.crop((0, pixels, img.width, img.height))


def trim_bottom(img: Image.Image, pixels: int) -> Image.Image:
    if pixels <= 0:
        return img
    if pixels >= img.height:
        raise ValueError(f"trim-last-bottom ({pixels}) is >= image height ({img.height}).")
    return img.crop((0, 0, img.width, img.height - pixels))


def build_default_output_folder(input_folder: Path) -> Path:
    return input_folder / f"{input_folder.name}_cut"


def load_images(input_folder: Path):
    image_paths = sorted(
        [p for p in input_folder.iterdir() if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS],
        key=lambda p: natural_sort_key(p.name),
    )
    if not image_paths:
        raise FileNotFoundError(f"No supported images found in: {input_folder}")

    images = []
    for path in image_paths:
        with Image.open(path) as img:
            images.append(img.convert("RGB"))
    return image_paths, images


def prepare_images(images, trim_first_top: int, trim_last_bottom: int):
    if not images:
        return [], 0

    target_width = min(img.width for img in images)
    normalized = [normalize_width(img, target_width) for img in images]

    prepared = []
    for idx, img in enumerate(normalized):
        current = img
        if idx == 0 and trim_first_top > 0:
            current = trim_top(current, trim_first_top)
        if idx == len(normalized) - 1 and trim_last_bottom > 0:
            current = trim_bottom(current, trim_last_bottom)
        prepared.append(current)

    return prepared, target_width


def infer_page_height(images, fallback: int = 2132) -> int:
    if not images:
        return fallback

    if len(images) >= 3:
        candidates = [img.height for img in images[1:-1]]
    else:
        candidates = [img.height for img in images]

    candidates = [h for h in candidates if h > 0]
    if not candidates:
        return fallback

    counts = Counter(candidates)
    mode_height, mode_count = counts.most_common(1)[0]
    median_height = int(round(statistics.median(candidates)))

    if mode_count >= max(2, len(candidates) // 2):
        return mode_height
    return median_height


def concatenate_images(images, width: int) -> Image.Image:
    total_height = sum(img.height for img in images)
    big_img = Image.new("RGB", (width, total_height), (255, 255, 255))
    y = 0
    for img in images:
        big_img.paste(img, (0, y))
        y += img.height
    return big_img


def mean_abs_diff(img_a: Image.Image, img_b: Image.Image) -> float:
    diff = ImageChops.difference(img_a, img_b)
    stat = ImageStat.Stat(diff)
    return sum(stat.mean) / len(stat.mean)


def overlap_band_stddev(gray_band: Image.Image) -> float:
    return ImageStat.Stat(gray_band).stddev[0]


def detect_bottom_overlap(
    prev_page: Image.Image,
    next_page: Image.Image,
    max_overlap_px: int,
    score_threshold: float,
    min_stddev: float,
) -> int:
    limit = min(max_overlap_px, prev_page.height - 1, next_page.height - 1)
    if limit <= 0:
        return 0

    best = 0
    for px in range(1, limit + 1):
        prev_band = prev_page.crop((0, prev_page.height - px, prev_page.width, prev_page.height))
        next_band = next_page.crop((0, 0, next_page.width, px))

        score = mean_abs_diff(prev_band, next_band)
        if score > score_threshold:
            continue

        if min_stddev > 0:
            prev_std = overlap_band_stddev(prev_band.convert("L"))
            next_std = overlap_band_stddev(next_band.convert("L"))
            if max(prev_std, next_std) < min_stddev:
                continue

        best = px

    return best


def iter_pages_from_strip(big_img: Image.Image, args):
    y = 0
    while y < big_img.height:
        chunk_bottom = min(y + args.page_height, big_img.height)
        page = big_img.crop((0, y, big_img.width, chunk_bottom))
        y += args.page_height

        if args.page_bottom_trim > 0 and page.height > args.page_bottom_trim:
            page = page.crop((0, 0, page.width, page.height - args.page_bottom_trim))

        if page.height <= 0:
            continue

        if args.skip_mostly_white_pages and is_mostly_white(page, args.white_ratio_threshold, args.white_cutoff):
            continue

        yield page


def save_pages_from_strip(big_img: Image.Image, output_folder: Path, args):
    output_folder.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    overlap_events = []

    page_iter = iter_pages_from_strip(big_img, args)
    current_page = next(page_iter, None)
    if current_page is None:
        return saved_paths, overlap_events

    page_idx = 1
    while True:
        next_page = next(page_iter, None)
        overlap_px = 0
        if next_page is not None and args.fix_bottom_overlap:
            overlap_px = detect_bottom_overlap(
                current_page,
                next_page,
                max_overlap_px=args.max_overlap_fix_px,
                score_threshold=args.overlap_fix_threshold,
                min_stddev=args.overlap_fix_min_std,
            )
            if overlap_px > 0 and current_page.height > overlap_px:
                current_page = current_page.crop((0, 0, current_page.width, current_page.height - overlap_px))
                overlap_events.append((page_idx, overlap_px))

        out_path = output_folder / f"page_{page_idx:03d}.jpg"
        current_page.save(out_path, "JPEG", quality=args.jpeg_quality)
        saved_paths.append(out_path)

        if next_page is None:
            break

        current_page = next_page
        page_idx += 1

    return saved_paths, overlap_events


def create_cbz(output_folder: Path, page_paths, cbz_name: str):
    cbz_path = output_folder / cbz_name
    with ZipFile(cbz_path, "w") as cbz:
        for page_path in page_paths:
            cbz.write(page_path, arcname=page_path.name)
    return cbz_path


def delete_files(paths, verbose: bool = False, label: str = "files"):
    deleted = 0
    for path in paths:
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"Warning: cannot delete {path}: {exc}")
    if verbose and deleted > 0:
        print(f"Deleted {deleted} {label}.")
    return deleted


def prompt_text(label: str, default: str = "", allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default:
            return default
        if allow_empty:
            return ""
        print("Value required.")


def prompt_int(label: str, default: int, min_value=None, max_value=None) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            value = default
        else:
            try:
                value = int(raw)
            except ValueError:
                print("Please enter an integer.")
                continue
        if min_value is not None and value < min_value:
            print(f"Value must be >= {min_value}.")
            continue
        if max_value is not None and value > max_value:
            print(f"Value must be <= {max_value}.")
            continue
        return value


def prompt_float(label: str, default: float, min_value=None, max_value=None) -> float:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            value = default
        else:
            try:
                value = float(raw)
            except ValueError:
                print("Please enter a number.")
                continue
        if min_value is not None and value < min_value:
            print(f"Value must be >= {min_value}.")
            continue
        if max_value is not None and value > max_value:
            print(f"Value must be <= {max_value}.")
            continue
        return value


def prompt_yes_no(label: str, default: bool) -> bool:
    default_char = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{default_char}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "o", "oui"}:
            return True
        if raw in {"n", "no", "non"}:
            return False
        print("Please answer yes or no.")


def prompt_mode(default_mode: str) -> str:
    mode_to_choice = {"images": "1", "cbz": "2", "both": "3"}
    choice_to_mode = {"1": "images", "2": "cbz", "3": "both"}
    default_choice = mode_to_choice.get(default_mode, "3")
    while True:
        print("Output mode: 1) images  2) cbz  3) both")
        raw = input(f"Choose mode [{default_choice}]: ").strip()
        choice = raw or default_choice
        if choice in choice_to_mode:
            return choice_to_mode[choice]
        print("Please choose 1, 2 or 3.")


def resolve_output_mode(args) -> str:
    if args.mode:
        return args.mode
    if args.cbz:
        return "both"
    return "images"


def configure_interactive(args):
    print("Interactive mode enabled.")
    source_default = args.input_folder or str(Path.cwd())

    while True:
        source_raw = prompt_text("Source folder", source_default)
        source_path = Path(source_raw).expanduser()
        if source_path.exists() and source_path.is_dir():
            break
        print("Source folder is invalid.")

    source_path = source_path.resolve()
    args.input_folder = str(source_path)

    default_output = build_default_output_folder(source_path)
    output_default = Path(args.output_folder).expanduser() if args.output_folder else default_output
    output_raw = prompt_text("Output folder", str(output_default))
    args.output_folder = str(Path(output_raw).expanduser().resolve())

    args.page_height = prompt_int("Page height (0 = auto)", args.page_height, min_value=0)
    args.trim_first_top = prompt_int("Trim top of first image", args.trim_first_top, min_value=0)
    args.trim_last_bottom = prompt_int("Trim bottom of last image", args.trim_last_bottom, min_value=0)
    args.page_bottom_trim = prompt_int("Trim bottom of each output page", args.page_bottom_trim, min_value=0)
    args.jpeg_quality = prompt_int("JPEG quality", args.jpeg_quality, min_value=1, max_value=100)

    mode = prompt_mode(resolve_output_mode(args))
    args.mode = mode
    args.cbz = mode in {"cbz", "both"}

    args.verbose = prompt_yes_no("Verbose logs", args.verbose)
    args.save_strip = prompt_yes_no("Save full concatenated strip (_strip.jpg)", args.save_strip)

    args.fix_bottom_overlap = prompt_yes_no("Auto-fix bottom pixel artifacts (1-6 px)", args.fix_bottom_overlap)
    if args.fix_bottom_overlap:
        args.max_overlap_fix_px = prompt_int("Max pixels to remove at page boundary", args.max_overlap_fix_px, min_value=1, max_value=24)
        args.overlap_fix_threshold = prompt_float("Boundary match threshold", args.overlap_fix_threshold, min_value=0.0, max_value=10.0)
        args.overlap_fix_min_std = prompt_float("Minimum texture stddev for boundary match", args.overlap_fix_min_std, min_value=0.0, max_value=100.0)

    args.skip_mostly_white_pages = prompt_yes_no("Skip mostly white pages", args.skip_mostly_white_pages)

    if mode in {"cbz", "both"}:
        cbz_default = args.cbz_name
        args.cbz_name = prompt_text("CBZ filename (empty = source folder name)", cbz_default, allow_empty=True)
        delete_pages_default = args.delete_pages_after_cbz
        if delete_pages_default is None:
            delete_pages_default = mode == "cbz"
        args.delete_pages_after_cbz = prompt_yes_no("Delete cut image files after CBZ creation", delete_pages_default)
        args.delete_source_after_cbz = prompt_yes_no(
            "Delete source image files after CBZ creation",
            args.delete_source_after_cbz,
        )
    else:
        args.cbz_name = ""
        args.delete_pages_after_cbz = False
        args.delete_source_after_cbz = False

    return args


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild pages by stacking all source images vertically, after trimming "
            "the first-top and last-bottom banners."
        )
    )
    parser.add_argument("input_folder", nargs="?", default=None, help="Input folder with JPG/WEBP/PNG images.")
    parser.add_argument("--interactive", action="store_true", help="Launch interactive prompts.")
    parser.add_argument("--output-folder", default="", help="Output folder. Default: <source>/<source_name>_cut.")

    parser.add_argument("--trim-first-top", type=int, default=786, help="Pixels removed from the top of the first image.")
    parser.add_argument("--trim-last-bottom", type=int, default=786, help="Pixels removed from the bottom of the last image.")

    parser.add_argument(
        "--page-height",
        type=int,
        default=2132,
        help="Output page height in px. Use 0 to auto-detect from source images.",
    )
    parser.add_argument("--page-bottom-trim", type=int, default=6, help="Pixels removed from the bottom of each output page.")
    parser.add_argument("--jpeg-quality", type=int, default=95, help="JPEG quality for output pages.")

    parser.add_argument("--skip-mostly-white-pages", action="store_true", help="Skip pages considered mostly white.")
    parser.add_argument("--white-ratio-threshold", type=float, default=0.98, help="Mostly-white threshold ratio.")
    parser.add_argument("--white-cutoff", type=int, default=245, help="Luma cutoff for white pixels.")

    parser.add_argument(
        "--fix-bottom-overlap",
        dest="fix_bottom_overlap",
        action="store_true",
        help="Detect and remove small duplicated bands (pixels parasites) at page boundaries.",
    )
    parser.add_argument(
        "--no-fix-bottom-overlap",
        dest="fix_bottom_overlap",
        action="store_false",
        help="Disable bottom overlap cleanup.",
    )
    parser.set_defaults(fix_bottom_overlap=True)
    parser.add_argument("--max-overlap-fix-px", type=int, default=6, help="Max pixels to remove per page boundary.")
    parser.add_argument("--overlap-fix-threshold", type=float, default=0.8, help="Max mean RGB diff to match boundary overlap.")
    parser.add_argument(
        "--overlap-fix-min-std",
        type=float,
        default=0.0,
        help="Min grayscale stddev for boundary matching (0 disables texture filtering).",
    )

    parser.add_argument("--save-strip", action="store_true", help="Also save the huge concatenated image as _strip.jpg.")
    parser.add_argument("--mode", choices=OUTPUT_MODES, default="", help="Output mode: images, cbz or both.")
    parser.add_argument("--cbz", action="store_true", help="Backward compatible shortcut for mode=both.")
    parser.add_argument("--cbz-name", default="", help="CBZ filename. Default: <input_folder_name>.cbz")

    parser.add_argument(
        "--delete-pages-after-cbz",
        dest="delete_pages_after_cbz",
        action="store_true",
        help="Delete generated page images after CBZ creation.",
    )
    parser.add_argument(
        "--keep-pages-after-cbz",
        dest="delete_pages_after_cbz",
        action="store_false",
        help="Keep generated page images after CBZ creation.",
    )
    parser.set_defaults(delete_pages_after_cbz=None)
    parser.add_argument(
        "--delete-source-after-cbz",
        action="store_true",
        help="Delete source image files after successful CBZ creation.",
    )

    parser.add_argument("--verbose", action="store_true", help="Print detailed processing info.")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.interactive or args.input_folder is None:
        args = configure_interactive(args)

    mode = resolve_output_mode(args)
    if args.delete_pages_after_cbz is None:
        args.delete_pages_after_cbz = mode == "cbz"

    input_folder = Path(args.input_folder).expanduser().resolve()
    if args.output_folder:
        output_folder = Path(args.output_folder).expanduser().resolve()
    else:
        output_folder = build_default_output_folder(input_folder).resolve()

    if not input_folder.exists() or not input_folder.is_dir():
        raise FileNotFoundError(f"Input folder not found: {input_folder}")
    if args.trim_first_top < 0 or args.trim_last_bottom < 0:
        raise ValueError("--trim-first-top and --trim-last-bottom must be >= 0.")
    if args.page_bottom_trim < 0:
        raise ValueError("--page-bottom-trim must be >= 0.")
    if args.page_height < 0:
        raise ValueError("--page-height must be >= 0.")
    if args.max_overlap_fix_px < 1:
        raise ValueError("--max-overlap-fix-px must be >= 1.")

    image_paths, images = load_images(input_folder)
    prepared_images, target_width = prepare_images(images, args.trim_first_top, args.trim_last_bottom)
    if not prepared_images:
        print("No valid image to process.")
        return

    if args.page_height == 0:
        args.page_height = infer_page_height(prepared_images)
    if args.page_height <= 0:
        raise ValueError("Auto-detected page height is invalid. Set --page-height manually.")

    big_img = concatenate_images(prepared_images, target_width)
    if args.save_strip:
        output_folder.mkdir(parents=True, exist_ok=True)
        strip_path = output_folder / "_strip.jpg"
        big_img.save(strip_path, "JPEG", quality=args.jpeg_quality)

    page_paths, overlap_events = save_pages_from_strip(big_img, output_folder, args)
    if not page_paths:
        print("No output page generated.")
        return

    print(f"Pages generated: {len(page_paths)} in {output_folder}")

    if args.verbose:
        print(f"Input images: {len(image_paths)}")
        print(f"Target width: {target_width}px")
        print(f"Trim first top: {args.trim_first_top}px")
        print(f"Trim last bottom: {args.trim_last_bottom}px")
        print(f"Page height: {args.page_height}px")
        print(f"Big strip size: {big_img.width}x{big_img.height}")
        print(f"Output mode: {mode}")
        if args.fix_bottom_overlap:
            total_px = sum(px for _, px in overlap_events)
            print(f"Bottom artifact fix: {len(overlap_events)} boundaries, {total_px}px removed.")
            for page_idx, px in overlap_events[:20]:
                print(f"  page_{page_idx:03d} -> page_{page_idx+1:03d}: -{px}px")
            if len(overlap_events) > 20:
                print(f"  ... {len(overlap_events) - 20} more boundaries")

    cbz_path = None
    if mode in {"cbz", "both"}:
        cbz_name = args.cbz_name.strip() or f"{input_folder.name}.cbz"
        cbz_path = create_cbz(output_folder, page_paths, cbz_name)
        print(f"CBZ generated: {cbz_path}")

        if args.delete_pages_after_cbz:
            delete_files(page_paths, verbose=args.verbose, label="cut page files")

        if args.delete_source_after_cbz:
            delete_files(image_paths, verbose=args.verbose, label="source image files")

    if mode == "images":
        print("Output contains image files only.")
    elif mode == "cbz":
        if args.delete_pages_after_cbz:
            print("Output contains CBZ only (cut images deleted).")
        else:
            print("Output contains CBZ and cut images.")
    elif mode == "both":
        print("Output contains cut images and CBZ.")


if __name__ == "__main__":
    main()
