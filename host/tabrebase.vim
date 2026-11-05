" Tab Rebase: highlighting and settings for the tab list.
" The host copies this next to the temp file and starts vim with -S, so
" nothing needs installing. Colors come from standard highlight groups,
" so it follows whatever colorscheme you use.
"
" Line format:
"   p <domain> <id> [#flags] [gN]  <title>  <url>  [DUPE ##|NEAR ##]

if !exists('g:syntax_on')
  syntax enable
endif
syntax clear

" --- view settings ---------------------------------------------------------
setlocal nowrap            " long lines scroll sideways instead of wrapping
set sidescroll=1           " ...one column at a time (zl/zh, zL/zH, zs/ze)
set sidescrolloff=8
setlocal cursorline
setlocal nospell
setlocal commentstring=#\ %s

" --- syntax ----------------------------------------------------------------
syn match tabComment  "^#.*"

" pick line: p -> domain -> id -> flags -> group
syn match tabPick     "^p\%[ick]\>"  nextgroup=tabDomain skipwhite
syn match tabDomain   "\S\+"         contained nextgroup=tabId skipwhite
syn match tabId       "\d\+"         contained nextgroup=tabFlags,tabGroup skipwhite
syn match tabFlags    "#[APUSMH]\+\>" contained nextgroup=tabGroup skipwhite
syn match tabGroup    "\<g\d\+\>"    contained

" anywhere on a line
syn match tabUrl      "\<\%(https\?\|file\|about\|moz-extension\|view-source\|chrome\):\S\+"
syn match tabDupe     "\s\zsDUPE \d\+$"
syn match tabNear     "\s\zsNEAR \d\+$"

" drop line: the whole line is dimmed, command stands out
syn match tabDropLine "^d\%[rop]\>.*" contains=tabDrop
syn match tabDrop     "^d\%[rop]\>"  contained

hi def link tabComment  Comment
hi def link tabPick     Statement
hi def link tabDomain   Identifier
hi def link tabId       Number
hi def link tabFlags    Special
hi def link tabGroup    Type
hi def link tabUrl      String
hi def link tabDupe     Error
hi def link tabNear     Todo
hi def link tabDropLine Comment
hi def link tabDrop     WarningMsg

let b:current_syntax = 'tabrebase'
