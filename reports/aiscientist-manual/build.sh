#!/usr/bin/env bash
# Build the AiScientist manuscript in both languages (PDF via tectonic, DOCX via pandoc).
set -euo pipefail
cd "$(dirname "$0")"
PDFOPTS=(--toc --toc-depth=2 -N -H header.tex
  -V geometry:margin=2.1cm -V fontsize=10pt -V colorlinks=true
  -V linkcolor=Maroon -V urlcolor=NavyBlue
  -V mainfont="Times New Roman" -V sansfont="Helvetica Neue" -V monofont="Menlo"
  -V CJKmainfont="Songti SC" -V CJKsansfont="PingFang SC" -V CJKmonofont="PingFang SC")
build () {  # $1 = md source, $2 = output basename
  pandoc "$1" -s -o "_$2.tex" "${PDFOPTS[@]}"
  python3 addrules.py "_$2.tex"
  tectonic -X compile "_$2.tex" --outdir . >/dev/null 2>&1
  mv "_$2.pdf" "$2.pdf"; rm -f "_$2.tex"
  pandoc "$1" -o "$2.docx" --toc --toc-depth=2 -N --reference-doc=ref-bordered.docx
  echo "$2.pdf  $(pdfinfo "$2.pdf" | awk '/^Pages/{print $2}') pages"
}
build manual.zh.md "AiScientist-技术报告-zh"
[ -f manual.en.md ] && build manual.en.md "AiScientist-Technical-Report-en"
