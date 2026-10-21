# Rule sets

Rule sets are grouped by the electrical standard they are written for, one folder per standard:

| Folder | Standard | Status |
|---|---|---|
| `bs7671/` | BS 7671 (NFE's default) | Draft small-power set for the pilot project's apartment type |
| `iec60364/` | IEC 60364 | To be added when a job needs it |
| `eas811/` | US EAS 811-1:2014 (Uganda) | To be added when a job needs it |

Each job records its governing standard: the `standard` argument of `Floor.open()`, the `--standard` option of the CLI, or the `standard` argument of the MCP `open_floor` tool. It defaults to BS 7671 and is written into `floor.json` and into every overlay's header. Every rules file declares the standard it was written for:

```yaml
name: BS 7671 - apartment type A - small power
standard: BS 7671
```

The toolkit refuses to apply a rule set written for a different standard from the job's.

None of these standards prescribes socket counts or positions. The rule sets hold NFE's own design rules for jobs under each standard, so the engineers own them and need to review every draft before it is relied on.
