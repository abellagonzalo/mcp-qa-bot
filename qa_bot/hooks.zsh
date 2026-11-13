# mcp-qa-bot shell hooks.
#
# Sourced once into the user's interactive zsh by open_session; the line is built by
# hooks.py (source_line), which owns this protocol on the Python side:
#
#   source hooks.zsh <runtime-dir> <wait-for-channel> <env key>...
#
# Every command line (typed by the agent or by a human) is wrapped in two invisible
# OSC sequences that travel through the pty together with the command's output:
#
#   ESC ] 777 ; qa ; start ; <id> ; <epoch> ; <command> BEL
#   ESC ] 777 ; qa ; end   ; <id> ; <exit code> ; <epoch> BEL
#
# Side channels (files in <runtime-dir>, never printed to the terminal):
#   state  idle | running | typing   (what the pane is doing right now)
#   done   id of the last finished command
#   env    KEY=value lines for the env keys given on the source line, refreshed at
#          every prompt
#
# After every command (and once right after sourcing, as the startup handshake) the
# precmd hook signals `tmux wait-for -S <channel>`.

typeset -g __qa_rt=$1 __qa_chan=$2
typeset -ga __qa_env_keys=(${@:3})
typeset -gi __qa_seq=0 __qa_cur=0
typeset -g __qa_state=''
zmodload zsh/datetime

__qa_set_state() {
  [[ $__qa_state == $1 ]] && return
  __qa_state=$1
  print -rn -- $1 >| $__qa_rt/state
}

__qa_preexec() {
  local cmd=${1:-$3}
  cmd=${cmd//$'\a'/}
  cmd=${cmd//$'\e'/}
  (( ++__qa_seq ))
  __qa_cur=$__qa_seq
  __qa_set_state running
  printf '\e]777;qa;start;%d;%s;%s\a' $__qa_cur $EPOCHREALTIME "$cmd"
}

# Order matters: the env and the idle state are published before `done`, so once
# Python sees `done` catch up with a command, the pane is already idle for the next one.
__qa_precmd() {
  local ec=$? fin=$__qa_cur
  if (( fin )); then
    printf '\e]777;qa;end;%d;%d;%s\a' $fin $ec $EPOCHREALTIME
    __qa_cur=0
  fi
  local k
  for k in $__qa_env_keys; do
    print -r -- "$k=${(P)k}"
  done >| $__qa_rt/env
  __qa_set_state idle
  (( fin )) && print -rn -- $fin >| $__qa_rt/done
  tmux wait-for -S $__qa_chan 2>/dev/null
}

# Publishes whether the human is halfway through typing (also across heredoc> lines).
__qa_line_redraw() {
  if [[ -n $BUFFER || -n $PREBUFFER ]]; then
    __qa_set_state typing
  else
    __qa_set_state idle
  fi
}

# Our precmd runs first and our preexec last, so plugin output (window titles, cwd
# reporting) falls outside the markers.
precmd_functions=(__qa_precmd ${precmd_functions:#__qa_precmd})
preexec_functions=(${preexec_functions:#__qa_preexec} __qa_preexec)

autoload -Uz add-zle-hook-widget
add-zle-hook-widget line-pre-redraw __qa_line_redraw
