#!/bin/sh
set -e

_auto_flag=""
[ "$conf_auto" = "true" ] &&\
  _auto_flag="-y"

echo "=== pipeline_random_image (Random Prompt -> Image Gen) ==="

echo "=== step 1/2: Generating Random Prompt ==="
out_path="$conf_prompt_file" $mrun_path/mrun_random_sentence $_auto_flag

prompt=$(cat "$conf_prompt_file")


echo "=== step 2/2: Image Gen ==="
mrun.py -m "$conf_image_gen_mode" $_auto_flag \
  -- \
    --prompt "${conf_prompt_head}${prompt}${conf_prompt_tail}" \
    --output "$conf_image_out_path"


echo "=== complete ==="

