#!/bin/sh
set -e

echo "=== pipeline_mi_qi2e_ltx2 (SD MOSS Image Gen -> SD Qwen Image 2 Edit -> SD LTX2 FLF2V) ==="

echo "=== step 1/3: SD MOSS Image Gen ==="
mrun.py -m sd_moss_i -y -- \
    --prompt "$_mrun_prompt_img_1" \
    --output "$_mrun_img_1_path"


echo "=== step 2/3: SD Qwen Image 2 Edit ==="
mrun.py -m sd_qi2_edit -y -- \
    --prompt "$_mrun_prompt_img_2" \
    --ref-image "$_mrun_img_1_path" \
    --output "$_mrun_img_2_path"


echo "=== step 3/3: SD LTX2 FLF2V ==="
mrun.py -m sd_ltx2_flf2v -y -- \
    --prompt "$_mrun_prompt_vid" \
    --init-img "$_mrun_img_1_path" \
    --end-img "$_mrun_img_2_path" \
    --output "$_mrun_vid_path"


echo "=== complete ==="

