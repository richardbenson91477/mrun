#!/bin/sh
set -e

echo "=== pipeline_mi_image_edit_to_video (Image Gen -> Image Edit -> FLF2V Video) ==="

echo "=== step 1/3: Image Gen ==="
mrun.py -m "$conf_image_gen_mode" -y -- \
    --prompt "$conf_prompt_img_1" \
    --output "$conf_img_1_path"


echo "=== step 2/3: Image Edit ==="
mrun.py -m "$conf_image_edit_mode" -y -- \
    --prompt "$conf_prompt_img_2" \
    --ref-image "$conf_img_1_path" \
    --output "$conf_img_2_path"


echo "=== step 3/3: FLF2V Video ==="
mrun.py -m "$conf_video_flf2v_mode" -y -- \
    --prompt "$conf_prompt_vid" \
    --init-img "$conf_img_1_path" \
    --end-img "$conf_img_2_path" \
    --output "$conf_vid_path"


echo "=== complete ==="

