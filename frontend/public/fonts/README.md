# Bundled font provenance

Spec 100 uses only locally bundled open fonts. Normal rendering must not fetch remote font stylesheets or assets.

## Inter

- Authors: The Inter Project Authors (`https://github.com/rsms/inter`)
- License: SIL Open Font License 1.1 (`OFL-Inter.txt`)
- Asset: `InterVariable.woff2`; SHA-256 `693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3`
- Used by the final operator shell to match the approved Inter typography.

## Instrument Sans

- Upstream: `Instrument/instrument-sans`
- Source commit: `7fa22308a3d0c94ee2b3cd537a1196b65db34a3e`
- License: SIL Open Font License 1.1 (`OFL-InstrumentSans.txt`)
- Required assets:
  - `InstrumentSans-Regular.woff2` ← `fonts/webfonts/InstrumentSans-Regular.woff2`, upstream blob `1e0a9719d54a0b5bb46ad8dabae3cad1a6eee203`
  - `InstrumentSans-Medium.woff2` ← `fonts/webfonts/InstrumentSans-Medium.woff2`, upstream blob `1360f22eac6514275f824c522e5329e3336928ea`
  - `InstrumentSans-SemiBold.woff2` ← `fonts/webfonts/InstrumentSans-SemiBold.woff2`, upstream blob `2285afc8e68c954a07bb438231acb1ca67afb3d2`

## IBM Plex Mono

- Upstream: `IBM/plex`
- Source commit: `bf260093582f04622aacc1e9f9ca604d7ccd0c42`
- License: SIL Open Font License 1.1 with Reserved Font Name "Plex" (`OFL-IBMPlexMono.txt`)
- Required asset:
  - `IBMPlexMono-Variable.woff2` ← `packages/plex-mono-variable/fonts/complete/woff2/IBM Plex Mono Var-Roman.woff2`, upstream blob `09a8c306fe9df33ffb8c2ff8edca7cb7025d79a2`

## Bricolage Grotesque

- Authors: The Bricolage Grotesque Project Authors (`https://github.com/ateliertriay/bricolage`)
- Distribution: fontsource 5.3.0, latin subset, variable axes wght/opsz/wdth
- License: SIL Open Font License 1.1 (`OFL-BricolageGrotesque.txt`)
- Asset: `BricolageGrotesque-latin.woff2`; SHA-256 `a756882d8a7802ebd4663b4544a62ebedafc7227c1c159ab95eea61393af3f8e`
- Used only by the spec 198 Lumen wordmark in the navigation rail.

## Latin Modern Math

- Authors: Bogusław Jackowski, Piotr Strzelczyk and Piotr Pianowski (GUST e-foundry), the OpenType math companion of the TeX Computer Modern/Latin Modern faces
- Distribution: CTAN `fonts/lm-math` version 1.959, `opentype/latinmodern-math.otf` (SHA-256 `6075562b771f8b82f0c179e363389684f2dd09de30038269e2628e504bd7be0f`), converted unchanged to WOFF2 with fontTools; the OpenType MATH table is kept
- License: GUST Font License (`GUST-LatinModernMath.txt`), an LPPL-based free font licence
- Asset: `LatinModernMath.woff2`; SHA-256 `fd0d512e707baa72a5558f19e7733918a54b9ada222f5c9297ab794be374ba69`
- Used by spec 230 for the derived equations (MathML) in the Workshop explanation, so they typeset in the LaTeX math face on every host.

No commercial, scraped, reverse-engineered, remote-CDN, or runtime-downloaded font is part of VISUAL-IDENTITY-1 or STUDIO-VISUAL-DESIGN-1.
