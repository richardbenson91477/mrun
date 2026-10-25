#!/bin/sh
set -e

auto_flag=""
[ "$_mrun_auto" = "true" ] &&\
  auto_flag="-y"

echo "=== pipeline_rand_image (Random Prompt -> Image Gen) ==="

echo "=== step 1/2: Generating Random Prompt ==="
out_path="$_mrun_prompt_file" $mrun_path/mrun_random_sentence $auto_flag

prompt=$(cat "$_mrun_prompt_file")


echo "=== step 2/2: Image Gen ==="
mrun.py -m "$_mrun_image_gen_mode" $auto_flag \
  -- \
    --prompt "${prompt} {_}" \
    --output "$_mrun_image_out_path"


echo "=== complete ==="

