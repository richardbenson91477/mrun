#!/bin/sh
set -e

_prompt="${_mrun_prompt_head} \"${_mrun_prompt_term1}\", \"${_mrun_prompt_term2}\", \"${_mrun_prompt_term3}\". ${_mrun_prompt_tail}"

mrun.py -m ll_cnv -y \
  -o "$_mrun_model_ol" -- \
    --prompt "$_prompt" \
    --ctx-size "$_mrun_ctx_size" \
    --reasoning "off" \
    --single-turn \
    "$@"

#_tmp_path="$(mktemp -u)"

