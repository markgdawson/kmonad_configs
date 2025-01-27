#!/usr/bin/env python3

import os
import re
import time
import json
import argparse
import random
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from PIL import Image, ImageDraw, ImageFont

##############################################################################
# Balanced Parsing for defsrc/deflayer
##############################################################################

def find_top_level_expression(content, start_index):
    """
    Parse a balanced s-expression beginning at content[start_index].
    Returns (expr_string, next_index).
    """
    stack = []
    i = start_index
    length = len(content)
    while i < length:
        if content[i] == '(':
            stack.append('(')
        elif content[i] == ')':
            if not stack:
                raise ValueError("Unmatched ')' at index {}".format(i))
            stack.pop()
            if not stack:
                return content[start_index:i+1], i+1
        i += 1
    if stack:
        raise ValueError("Unmatched '(' in expression starting at index {}".format(start_index))
    return None, length


def extract_defblocks(content, block_type="defsrc"):
    """
    Extract top-level s-expressions of the form (defsrc ...) or (deflayer name ...).
    Returns:
      - for defsrc: a list of (None, body_string) (likely just one item),
      - for deflayer: a list of (layer_name, body_string).
    """
    results = []
    idx = 0
    while True:
        found = content.find(f"({block_type}", idx)
        if found == -1:
            break
        try:
            expr_string, next_idx = find_top_level_expression(content, found)
        except ValueError as e:
            print(f"Warning: {e}")
            idx = found + 1
            continue

        inner = expr_string.strip()[1:-1].strip()  # remove outer parentheses
        parts = inner.split(None, 1)
        if len(parts) < 2:
            idx = next_idx
            continue
        block_keyword = parts[0]
        remainder = parts[1].strip()
        if block_keyword == "defsrc" and block_type == "defsrc":
            results.append((None, remainder))
        elif block_keyword == "deflayer" and block_type == "deflayer":
            # remainder should start with layerName
            remainder_parts = remainder.split(None, 1)
            if not remainder_parts:
                idx = next_idx
                continue
            layer_name = remainder_parts[0]
            layer_body = remainder_parts[1] if len(remainder_parts) > 1 else ""
            results.append((layer_name, layer_body))

        idx = next_idx
    return results


def find_top_level_expression_in_body(body, start_index):
    """
    Helper for split_top_level_forms.
    """
    return find_top_level_expression(body, start_index)


def split_top_level_forms(body):
    """
    Split a string with multiple tokens/s-expressions into a list of top-level forms.
    e.g. "abc (tap-hold 200 a (around lsft b)) def"
      -> ["abc", "(tap-hold 200 a (around lsft b))", "def"]
    """
    forms = []
    i = 0
    body_len = len(body)
    while i < body_len:
        while i < body_len and body[i].isspace():
            i += 1
        if i >= body_len:
            break
        if body[i] == '(':
            try:
                expr_string, next_i = find_top_level_expression_in_body(body, i)
                forms.append(expr_string)
                i = next_i
            except ValueError as e:
                # If unmatched, just take the rest
                forms.append(body[i:].strip())
                break
        else:
            # parse a token until whitespace
            start = i
            while i < body_len and not body[i].isspace():
                i += 1
            token = body[start:i]
            forms.append(token)
    return forms


def parse_kmonad_config(filepath):
    """
    Return (defsrc_list, layer_dict).
      defsrc_list: list of top-level forms in defsrc
      layer_dict: {layerName -> list of top-level forms in that layer's body}
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # Remove block comments and line comments
    content = re.sub(r"#\|.*?\|#", "", content, flags=re.DOTALL)
    content = re.sub(r";;.*?$", "", content, flags=re.MULTILINE)

    # defsrc
    defsrc_blocks = extract_defblocks(content, block_type="defsrc")
    defsrc_list = []
    if defsrc_blocks:
        # Just take the first
        _, defsrc_body = defsrc_blocks[0]
        defsrc_list = split_top_level_forms(defsrc_body)

    # deflayer
    deflayer_blocks = extract_defblocks(content, block_type="deflayer")
    layer_dict = {}
    for (layer_name, layer_body) in deflayer_blocks:
        forms = split_top_level_forms(layer_body)
        layer_dict[layer_name] = forms

    return defsrc_list, layer_dict


##############################################################################
# 2) Optional s-expression -> short label mapping
##############################################################################

def get_short_label(original: str, mapping: dict) -> str:
    """
    If `original` is in `mapping`, return mapping[original], else original.
    """
    stripped = original.strip()
    return mapping.get(stripped, stripped)

##############################################################################
# 3) Physical Layout Indirection
##############################################################################
# We'll define an ANSI-like layout. Each entry is a canonical name -> (x,y).
# Any name from defsrc that exactly matches a key in this dictionary
# is displayed in that location. If defsrc has "1", it goes to (1,0).
# If a key is physically present (like "esc") but not in defsrc, it appears
# greyed-out as "(Not in defsrc)" in the final image.

PHYSICAL_KEY_MAP = {
    # row 0
    "esc": (0, 0), "1": (1, 0), "2": (2, 0), "3": (3, 0), "4": (4, 0),
    "5": (5, 0), "6": (6, 0), "7": (7, 0), "8": (8, 0), "9": (9, 0),
    "0": (10,0), "-": (11,0), "=": (12,0), "bspc": (13,0),

    # row 1
    "tab": (0,1), "q": (1,1), "w": (2,1), "e": (3,1), "r": (4,1),
    "t": (5,1), "y": (6,1), "u": (7,1), "i": (8,1), "o": (9,1),
    "p": (10,1), "[": (11,1), "]": (12,1), "\\": (13,1),

    # row 2
    "caps": (0,2), "a": (1,2), "s": (2,2), "d": (3,2), "f": (4,2),
    "g": (5,2), "h": (6,2), "j": (7,2), "k": (8,2), "l": (9,2),
    ";": (10,2), "'": (11,2), "ret": (12,2),

    # row 3
    "lsft": (0,3), "z": (1,3), "x": (2,3), "c": (3,3), "v": (4,3),
    "b": (5,3), "n": (6,3), "m": (7,3), ",": (8,3), ".": (9,3),
    "/": (10,3), "rsft": (11,3),

    # row 4
    "lctl": (0,4), "lmet": (1,4), "lalt": (2,4), "spc": (3,4),
    "ralt": (4,4), "rmet": (5,4), "cmp": (6,4), "rctl": (7,4),
}

# We'll find the bounding box from PHYSICAL_KEY_MAP so we know how many columns/rows:
max_x = max(pos[0] for pos in PHYSICAL_KEY_MAP.values())
max_y = max(pos[1] for pos in PHYSICAL_KEY_MAP.values())
physical_cols = max_x + 1
physical_rows = max_y + 1


##############################################################################
# 4) Generating a Single Image with Multiple Layer Assignments per Key
##############################################################################

def generate_single_grid_image(defsrc_list, layer_dict, expr_map, out_path="kmonad_layout.png"):
    """
    1. We find each defsrc key in PHYSICAL_KEY_MAP. That gives us an index in defsrc_list
       => we can retrieve that index from each layer.
    2. For each physical key in PHYSICAL_KEY_MAP, we gather all layers' assigned values
       if it appears in defsrc (otherwise it's "Not in defsrc").
    3. We draw a single rectangle for each physical key, listing one line per layer
       in that rectangle, each line in the layer's color. Also show the physical key name.
    4. We draw a legend for the layers & their colors.
    5. If the final short label is more than 10 chars, print it to stdout so the user can
       add it to their alias file if they want.
    6. For any assignment "XX", we display a blank instead of "XX".
    """

    # 1) Build a mapping from defsrc key -> index
    defsrc_index_for_key = {}
    for i, keyname in enumerate(defsrc_list):
        # if it matches a key in PHYSICAL_KEY_MAP, store i
        if keyname in PHYSICAL_KEY_MAP:
            defsrc_index_for_key[keyname] = i

    # Sort layer names
    layer_names = sorted(layer_dict.keys())
    # Assign each layer a color
    base_colors = [
        (230, 0, 0),
        (0, 180, 0),
        (0, 0, 230),
        (200, 120, 0),
        (180, 0, 180),
        (0, 160, 160),
        (120, 120, 50),
        (120, 50, 120),
        (100, 100, 200),
        (200, 100, 100)
    ]
    # If we have more layers than base_colors, pick random for extras
    layer_colors = {}
    idx = 0
    import random
    for ln in layer_names:
        if idx < len(base_colors):
            layer_colors[ln] = base_colors[idx]
        else:
            r = random.randint(0,200)
            g = random.randint(0,200)
            b = random.randint(0,200)
            layer_colors[ln] = (r, g, b)
        idx += 1

    # We'll produce a grid of physical_rows x physical_cols
    cell_width = 130
    # Increase the cell height to 140, as requested:
    cell_height = 180
    # We also need space for a legend at the top
    legend_height = 50 + ((len(layer_names) + 3)//4)*20
    image_width = physical_cols * cell_width
    image_height = physical_rows * cell_height + legend_height

    img = Image.new("RGB", (image_width, image_height), color=(230,230,230))
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.truetype("Arial.ttf", 16)
        small_font = ImageFont.truetype("Arial.ttf", 14)
    except:
        font = ImageFont.load_default()
        small_font = ImageFont.load_default()

    # 2) Draw Legend at the top
    legend_x = 10
    legend_y = 10
    for i, ln in enumerate(layer_names):
        lx = legend_x + (i % 4)*200
        ly = legend_y + (i // 4)*20
        color = layer_colors[ln]
        draw.rectangle([lx, ly, lx+20, ly+15], fill=color)
        draw.text((lx+25, ly), ln, font=small_font, fill=(0,0,0))
    grid_offset_y = legend_height

    # 3) For each physical key in PHYSICAL_KEY_MAP
    for keyname, (cx, cy) in PHYSICAL_KEY_MAP.items():
        px = cx * cell_width
        py = grid_offset_y + cy * cell_height

        draw.rectangle([px, py, px+cell_width-2, py+cell_height-2],
                       outline=(0,0,0), width=1)

        # check if it's in defsrc
        if keyname not in defsrc_index_for_key:
            draw.text((px+5, py+5), keyname, font=font, fill=(150,150,150))
            draw.text((px+5, py+30), "(Not in defsrc)", font=small_font, fill=(120,120,120))
        else:
            draw.text((px+5, py+5), keyname, font=font, fill=(0,0,0))
            i = defsrc_index_for_key[keyname]
            line_y = py + 30

            # gather each layer's assignment
            for ln in layer_names:
                color = layer_colors[ln]
                layer_keys = layer_dict[ln]
                if i < len(layer_keys):
                    assigned = layer_keys[i]
                else:
                    assigned = "(N/A)"

                # For "XX" -> blank
                if assigned == "XX":
                    assigned = ""

                # Possibly apply short label
                short_assigned = get_short_label(assigned, expr_map)

                # If short_assigned == "XX", also replace with blank:
                if short_assigned == "XX":
                    short_assigned = ""

                # If final label is > 10 chars, print to stdout
                if len(short_assigned) > 10:
                    print(f"Long label found (>{len(short_assigned)} chars): {short_assigned}")

                draw.text((px+5, line_y), short_assigned, font=small_font, fill=color)
                line_y += 15

    img.save(out_path)
    print(f"Generated layout image: {out_path}")

##############################################################################
# 5) Watchdog
##############################################################################

class ConfigChangeHandler(FileSystemEventHandler):
    def __init__(self, kbd_path, expr_map):
        super().__init__()
        self.kbd_path = kbd_path
        self.expr_map = expr_map

    def on_modified(self, event):
        if event.is_directory:
            return
        if os.path.abspath(event.src_path) == os.path.abspath(self.kbd_path):
            print("KMonad config changed; regenerating layout image...")
            defsrc_list, layer_dict = parse_kmonad_config(self.kbd_path)
            generate_single_grid_image(defsrc_list, layer_dict, self.expr_map, "kmonad_layout.png")


##############################################################################
# 6) Main
##############################################################################

def main():
    parser = argparse.ArgumentParser(description="Watch a KMonad .kbd file and generate a single-grid crib-sheet showing all layers in different colors.")
    parser.add_argument("kbd_path", help="Path to KMonad .kbd file")
    parser.add_argument("--map-file", help="Optional JSON file mapping s-expressions to short labels", default=None)
    args = parser.parse_args()

    expr_map = {}
    if args.map_file and os.path.isfile(args.map_file):
        try:
            with open(args.map_file, "r", encoding="utf-8") as f:
                expr_map = json.load(f)
            print(f"Loaded expression map from {args.map_file}")
        except Exception as e:
            print(f"Warning: could not load map file: {e}")

    # Initial parse & image
    defsrc_list, layer_dict = parse_kmonad_config(args.kbd_path)
    generate_single_grid_image(defsrc_list, layer_dict, expr_map, "kmonad_layout.png")

    # Watch
    handler = ConfigChangeHandler(args.kbd_path, expr_map)
    observer = Observer()
    watch_dir = os.path.dirname(os.path.abspath(args.kbd_path)) or "."
    observer.schedule(handler, path=watch_dir, recursive=False)
    observer.start()
    print(f"Watching {args.kbd_path} for changes... (Ctrl+C to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
