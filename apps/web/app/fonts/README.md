# Bundled fonts

The Web build uses these local fonts and does not contact Google Fonts.
Both are distributed under the SIL Open Font License 1.1; see
`public/fonts/Inter-OFL.txt` and `public/fonts/JetBrainsMono-OFL.txt`.
Keeping licenses in `public` includes them in Docker and standalone distributions.

Sources downloaded from the Google Fonts repository on 2026-09-05:

- Inter: https://github.com/google/fonts/blob/main/ofl/inter/Inter%5Bopsz,wght%5D.ttf
- JetBrains Mono: https://github.com/google/fonts/blob/main/ofl/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf

The complete variable TTF files were converted to WOFF2 with FontTools and
Brotli (`font = TTFont(source); font.flavor = "woff2"; font.save(destination)`).
No glyphs were removed. Only the WOFF2 output is needed by the application;
the conversion tools are not application or build dependencies.
