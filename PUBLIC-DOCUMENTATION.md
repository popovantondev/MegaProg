# Public documentation maintenance

`public-release.json` contains the public version, platform, release status, exact application filenames, requirements and localized navigation. Detailed guides and legal notices retain their own content.

After a reviewed metadata change, run `python tools/check_public_docs.py --write`. Review the generated README summaries, guides and `docs/release-notes.*.md`. The three release-note files provide the same five sections for future releases: changes, compatibility, installation, limitations and checksums. Replace the commit-list link with the reviewed changelog for that release before publication. These files do not update an existing GitHub release or archive.

Run `python tools/check_public_docs.py --online --published` to check local pages, application version declarations, published release assets and localized HTML guide availability. The Public documentation workflow runs on pull requests, pushes and weekly. A green documentation check does not verify application behavior, signing or a clean-machine installation.

Preview links target the exact reviewed release. Source-only projects have no binary download button. Published archives, licenses and repository visibility require their own review.

Temporary technical results belong in Actions artifacts. Do not use user-facing Releases for diagnostic self-tests. Screenshots and attachments must exclude credentials and private user content; label demonstration data honestly.
