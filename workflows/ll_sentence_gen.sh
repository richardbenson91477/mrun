#!/bin/sh
set -e

prompt="${_mrun_prompt_head} ${_mrun_prompt_terms}. ${_mrun_prompt_tail}"

tmp_log_path="$(mktemp)"


mrun.py -m ll_cnv -y \
  -o "$_mrun_model_ol" -- \
    --prompt "$prompt" \
    --ctx-size "$_mrun_ctx_size" \
    --reasoning "off" \
    --single-turn \
    "$@" | tee "$tmp_log_path"


echo "$0: full ll_cnv output copied to \"$tmp_log_path\""


[ "$_mrun_out_path" != "/dev/null" ] && {
    prompt_ln=$(grep -n "$_mrun_prompt_tail" "$tmp_log_path" |
          cut -d':' -f1
          )

    tail -n +"$((prompt_ln + 1))" "$tmp_log_path" |
          head -n 1 \
          > "$_mrun_out_path"

    echo "$0: response copied to \"$_mrun_out_path\""
    }

