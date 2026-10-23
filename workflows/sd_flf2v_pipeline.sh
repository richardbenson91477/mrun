#!/bin/sh
set -e

# Ensure sub-steps don't pause for confirmation
export MRUN_YES=1

echo "=== Step 1: Generating base image ==="
mrun.py -m sd_moss_i -T -- \
    --output "$BASE_IMG" \
    --prompt "$PROMPT_BASE"

echo "=== Step 2: Editing image to add text ==="
mrun.py -m sd_qi2_edit -T -- \
    --ref-image "$BASE_IMG" \
    --output "$EDIT_IMG" \
    --prompt "$PROMPT_EDIT"

echo "=== Step 3: Generating video ==="
mrun.py -m sd_ltx2_flf2v -T -- \
    --init-img "$BASE_IMG" \
    --end-img "$EDIT_IMG" \
    --output "$FINAL_VID" \
    --prompt "$PROMPT_VID"

echo "=== Pipeline Complete ==="