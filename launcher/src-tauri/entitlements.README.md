# entitlements.plist

**Do not add XML comments to `entitlements.plist`.** `codesign` parses it with
AMFI's stricter parser, which rejects `<!-- -->` anywhere in the file and fails
the build with:

```
Failed to parse entitlements: AMFIUnserializeXML: syntax error near line N
```

`plutil -lint` reports the file as OK regardless, so it does not catch this.
The rationale for each entitlement lives in `docs/macos-signing.md` for exactly
this reason — it cannot live in the file itself.
