# VISUAL_DIRECTION — JarvisOS 100

This document translates the maintainer's qualitative visual preferences into design constraints for `100 VISUAL-IDENTITY-1`.

## One-sentence direction

**A light-first, information-dense engineering workstation whose visual language treats advanced computation as an extension of natural evolution: mineral light surfaces, aqueous-chlorophyll accents, precise engineering structure, restrained organic geometry, subtle material depth and a dignified Jarvis presence.**

## Mental model

JarvisOS should feel like a technological organism created by humans as natural beings: nature has evolved to the point where it can build tools that investigate nature more deeply. Jarvis and the other agents inhabit the workspace as an ecosystem of intelligences able to observe the same engineering system and help explore what can be created.

The intended emotional blend is:

- **Pandora:** interconnection, chlorophyll, water, biological life, technology that feels integrated with living systems;
- **Olympus:** marble/mineral light, proportion, permanence, quiet monumentality, heroic dignity;
- **mature solarpunk:** optimistic advanced technology integrated with the physical world, without neon or eco-kitsch;
- **engineering workstation:** dense information, precision, explicit state, trustworthy controls and fast scanning.

The classical/natural references are atmospheric, never literal. Do not add columns, laurel motifs, leaves, vines, gods, runes or decorative biomechanical ornament to engineering controls.

## Character axes

| Axis | Direction |
| --- | --- |
| Nature ↔ industrial | 9/10 toward living technology; engineering discipline remains in layout |
| Humanistic ↔ mechanical | humanistic visual expression around rational engineering content |
| Futuristic ↔ contemporary | quietly advanced; should feel newer than current engineering software without sci-fi decoration |
| Dense ↔ spacious | medium-high density, never cramped and never SaaS-empty |
| Organic ↔ geometric | hybrid `bio-machined`: precise structure, selectively softer floating/transient surfaces |
| Flat ↔ dimensional | low-amplitude depth through border/elevation/shadow; no heavy skeuomorphism |
| Static ↔ animated | restrained motion; interaction transitions should be perceived subconsciously |
| Light ↔ dark | light canonical; dark optional and fully supported |

## Desired first impression

A technical user should think, in this order:

1. **This is a serious and functional engineering instrument.**
2. **It is contemporary and carefully designed rather than carrying twenty-year-old engineering-software graphics.**
3. **The visual hierarchy makes the system easier to understand instinctively.**
4. Only after that should the user notice the unusual living/natural character.

The target is not “wow, futuristic UI”. The visual identity succeeds when craft improves comprehension.

## External reference matrix

Ratings are maintainer aesthetic ratings, not source quality scores. References are inspiration only; do not copy brand assets, layouts or unverified/proprietary fonts.

| Reference | Maintainer rating | Take | Explicitly do not take |
| --- | ---: | --- | --- |
| Bioo | 8.5/10 | art direction; natural/technological image mood; biological color atmosphere | black marketing-site composition as application UI; large white-on-black typography |
| Generate:Biomedicines | 6/10 | no material authority beyond broad biotech context | no need to imitate typography or palette |
| Recursion | 7.5/10 | calm, readable body-type character; scientific seriousness | logo and color system |
| Heirloom Carbon | 9/10 | strongest mood reference for nature + engineering; typography character; mineral/natural palette; quiet seriousness | exact brand reproduction; insufficiently verified font identity |
| AIR COMPANY | 8+/10 | clean premium typography and industrial/climate-tech restraint | no meaningful palette authority; its Suisse Int'l web typeface is commercial and is reference-only |
| Linear | 8.5/10 | type clarity, dense polished product craft, subtle hierarchy | black-first identity; developer-SaaS visual identity |
| Raycast | 9/10 | highest component-craft reference: buttons, floating surfaces, microinteraction polish | excessive Apple resemblance, excessive gloss/glow, dark-first styling |
| Vercel | 8.5/10 | typography discipline and restrained motion | black/white-first identity and developer-brand austerity |
| Frontier | 6/10 | no material positive authority | typography and general visual direction |

Reference URLs and provenance notes are recorded in `docs/audits/VISUAL_IDENTITY_REFERENCE_AUDIT_2026-08-25.md` (not carried into this repository).

## Visual vocabulary

Use these ideas as implementation guidance:

- mineral white, limestone, warm mist, pale stone;
- chlorophyll seen through water or a translucent leaf;
- graphite for technical structure;
- sunlight rather than glow;
- low, soft elevation rather than floating-card spectacle;
- engineered seams/borders rather than large filled accent blocks;
- biological softness only where the surface is transient, assistive or alive;
- clear tabular/technical alignment inside humanistic surrounding typography.

## Bio-machined geometry

Do not choose between “rounded organic” and “square technical” globally. Use geometry to encode surface role.

- **Canvas frames, tables, navigators, Properties, Analysis Dock, dense engineering panels:** low-to-moderate radius and architectural alignment.
- **Buttons and inputs:** modest radius; comfortable but not pill-like.
- **Jarvis proposal surfaces, command palette, popovers, transient HUDs:** slightly softer radius and potentially light translucency/elevation.
- **Pills:** reserved for tags, status chips, segmented/toggle semantics where the shape is meaningful.

The goal is a precise machine in which selected assistive surfaces feel grown rather than stamped out.

## Jarvis visual presence

Jarvis is the workspace's expert colleague/butler/secretary, analogous to the role of Jarvis for Tony Stark. It is not a separate chatbot product embedded in JarvisOS.

Therefore:

- same design language as the rest of the workstation;
- may receive slightly richer depth, accent modulation and waiting-state motion;
- no purple gradient, sparkle iconography or “magic AI” branding;
- proposal/action surfaces remain explicit and deterministic in their affordances;
- AI waiting animation may be calmer and more organic than ordinary loading, but only while real work is pending.

## Forbidden directions

Do not drift into:

- neon, cyberpunk or gamer HUD styling;
- petroleum/navy-teal identity;
- black-first canonical UI;
- purple AI gradients or sparkles;
- generic ultra-rounded SaaS cards;
- glassmorphism across structural engineering panels;
- literal eco/nature decoration;
- fake marble texture behind tables or controls;
- dense legacy HYSYS-style microtype;
- oversized modern-SaaS whitespace;
- Apple clone styling;
- color used as the only carrier of engineering/status meaning.
