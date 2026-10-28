#!/bin/sh
set -e

_prompt="${conf_prompt_head} ${conf_prompt_terms}. ${conf_prompt_tail}"

_tmp_log_path="$(mktemp)"


mrun.py -m "ll_cnv" -y \
  -o "$conf_model_ol" -- \
    --prompt "$_prompt" \
    --ctx-size "$conf_ctx_size" \
    --reasoning "off" \
    --single-turn \
    "$@" | tee "$_tmp_log_path"


echo "$0: full \"ll_cnv\" output copied to \"$_tmp_log_path\""


[ "$conf_out_path" != "/dev/null" ] && {
    prompt_ln=$(grep -n "$conf_prompt_tail" "$_tmp_log_path" |
          cut -d':' -f1
          )

    tail -n +"$((prompt_ln + 1))" "$_tmp_log_path" |
          head -n 1 \
          > "$conf_out_path"

    echo "$0: response copied to \"$conf_out_path\""
    }

