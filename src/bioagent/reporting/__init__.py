"""Turning a finished run into deliverables: the manuscript render (pandoc, PDF/DOCX), the
categorized result bundle, the reference list, and the vision-model render review.

Platform code. It used to sit under ``tools/`` although the model never calls it; it moved here so
that ``tools/`` holds only model-callable tools (the future AiScientist-tools package).
"""
