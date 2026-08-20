# UI prototypes

This directory contains design references only. Nothing here is mounted by
`orbit serve`, imported by the production package, or allowed to mutate the
Runtime.

`runtime-ui.html` is the exact interactive mock introduced in commit
`ad33217` and removed from the production static tree in commit `b73c810`.
It is retained here as the visual baseline for the Runtime UI rebuild.

`goal-detail/` is the design study the History drawer was built from:
`code.html` is the marked-up screen and `screen.png` is how it was meant to
look. Its palette and typefaces are the study's own and were never adopted;
what the Runtime took from it is the layout, which `views/index.js` and
`views.css` both cite by name. The `DESIGN.md` that came with it is not here
— this repository keeps design specs local by rule, see `.gitignore`.
