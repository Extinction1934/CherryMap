# CherryMap

Source of truth for the published app, including Interconnect 2.0.

The files in this repository are the application itself: client, published API bridge, Python TPM-119 bridge, configs, and the geographic index.

`node_modules` is not in the git tree. GitHub rejects that folder because it is larger than the file limit. The full dependency set is the release asset `CherryMap-complete-latest.zip.gz` on the `interconnect-2026-10-26` release. After downloading it:

```
gunzip CherryMap-complete-latest.zip.gz
unzip CherryMap-complete-latest.zip
mv libraries node_modules
```

Inside that archive, `libraries/` is the `node_modules` tree.
