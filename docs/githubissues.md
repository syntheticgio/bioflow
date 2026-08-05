## 📋 GitHub Issues — Triage & Classification

### By Type

| Type | Count | Issues |
|------|-------|--------|
| **Feature** | 19 | #1, #3, #4, #5, #6, #7, #9, #12, #13, #14, #16, #17, #18, #20, #21, #22, #23, #24, #25, #26, #28, #29, #30, #31, #32, #33, #36, #37, #38, #39, #40, #41, #42, #43, #44, #45 |
| **Bug** | 1 | #10 |
| **Maintenance** | 5 | #8, #11, #15, #19, #27, #34, #46 |
| **Epic** (umbrella) | 9 | #3, #4, #5, #6, #7, #13, #14, #18, #30, #41 |

### By Priority

| Priority | Count | Issues |
|----------|-------|--------|
| 🔴 **High** | 6 | #4, #7, #10, #11, #15, #22, #37, #44 |
| 🟡 **Medium** | 14 | #5, #6, #9, #13, #14, #18, #20, #21, #23, #24, #25, #26, #28, #29, #31, #32, #36, #38, #39, #41, #42, #43, #45, #46 |
| 🟢 **Low** | 7 | #1, #3, #8, #12, #16, #17, #19, #27, #33, #34, #40 |

### By Area

| Area | Count | Issues |
|------|-------|--------|
| **backend** | 17 | #1, #3, #7, #8, #9, #10, #11, #16, #18, #19, #20, #22, #24, #25, #27, #30, #31, #32, #33, #36, #41, #42, #44, #45 |
| **frontend** | 7 | #9, #15, #18, #30, #32, #33, #34, #36 |
| **pipelines** | 16 | #5, #6, #10, #11, #12, #13, #14, #15, #17, #18, #20, #21, #23, #24, #26, #30, #40, #41, #43, #44 |
| **infrastructure** | 12 | #4, #5, #7, #22, #28, #29, #30, #31, #37, #38, #39, #40, #41, #42, #45, #46 |
| **provenance** | 3 | #8, #9, #16 |
| **profiles** | 2 | #3, #25 |

---

### 🔴 High Priority (6 issues)

| # | Title | Type | Areas |
|---|-------|------|-------|
| **#4** | Build a native BioFlow installer and launcher | Feature/Epic | infrastructure |
| **#7** | Add configurable resource limits and intelligent enforcement | Feature/Epic | backend, infrastructure |
| **#10** | Investigate disappearing QC report directories | Bug | backend, pipelines |
| **#11** | Audit hand-maintained tool registries | Maintenance | backend, pipelines |
| **#15** | Verify the in-app DESeq2 workflow end to end | Maintenance | frontend, pipelines |
| **#22** | Add persisted global resource limit settings | Feature | backend, infrastructure |
| **#37** | Publish BioFlow container images to GHCR | Feature | infrastructure |
| **#44** | Compress FASTQ at end of SRA download before staging | Feature | backend, pipelines |

### 🟡 Medium Priority (14 issues)

| # | Title | Type | Areas |
|---|-------|------|-------|
| **#5** | Support post-install downloads for optional tools | Feature/Epic | pipelines, infrastructure |
| **#6** | Add job progress reporting and resource transparency | Feature/Epic | backend, pipelines |
| **#9** | Add per-object computation provenance | Feature | frontend, backend, provenance |
| **#13** | Complete remaining post-assembly QC workflows | Feature/Epic | pipelines |
| **#14** | Add reference-guided assembly workflows | Feature/Epic | pipelines |
| **#18** | Add reusable user-defined pipeline DAGs | Feature/Epic | frontend, backend, pipelines |
| **#20** | Define DAG persistence and run-instance model | Feature | backend, pipelines |
| **#21** | Add reference-based assembly tooling | Feature | pipelines |
| **#23** | Add Pilon reference-guided polishing workflow | Feature | pipelines |
| **#24** | Define and expose a common per-job progress model | Feature | backend, pipelines |
| **#25** | Define profile file-sharing policy and revoke flow | Feature | backend, profiles |
| **#26** | Plan optional-tool delivery architecture | Feature | pipelines, infrastructure |
| **#28** | Define native launcher contract | Feature | infrastructure |
| **#29** | Migrate volume | Feature | infrastructure |
| **#31** | MCP server | Feature | infrastructure |
| **#32** | Add AI narrative summaries for DE and variant-call results | Feature | frontend, backend |
| **#36** | Add project Q&A chat panel | Feature | frontend, backend |
| **#38** | Add CI/CD workflow to build and publish images | Feature | infrastructure |
| **#39** | Package, sign, and distribute native launcher | Feature | infrastructure |
| **#41** | Compress stored objects where format allows | Feature/Epic | backend, pipelines, infrastructure |
| **#42** | Decide object compression policy | Feature | backend, infrastructure |
| **#43** | Register pigz as a pipeline tool | Feature | pipelines, infrastructure |
| **#45** | Apply compression policy to upload and pipeline-output ingest | Feature | backend, infrastructure |
| **#46** | Attach amd64 runner and publish linux/amd64 images | Maintenance | infrastructure |

### 🟢 Low Priority (7 issues)

| # | Title | Type | Areas |
|---|-------|------|-------|
| **#1** | Notify on new feedback submissions | Feature | backend |
| **#3** | Share files between profiles without copying bytes | Feature/Epic | backend, profiles |
| **#8** | Segment timing models by thread count | Maintenance | backend, provenance |
| **#12** | Add DRAGMAP aligner support | Feature | pipelines |
| **#16** | Generate fact-grounded pipeline provenance narratives | Feature | backend, provenance |
| **#17** | Improve paired-read detection beyond filenames | Feature | pipelines |
| **#19** | Make feedback submission ordering deterministic | Maintenance | backend |
| **#27** | Add execution links from roadmap epics to first-slice child issues | Maintenance | backend |
| **#33** | Add on-demand AI explanation for job failures | Feature | frontend, backend |
| **#34** | Add ARIA combobox semantics to ModelCombo and NcbiDownloadDialog | Bug/Maintenance | frontend |
| **#40** | Revisit installer pre-pull of optional tool images | Feature | pipelines, infrastructure |

---

### 🔗 Dependency Chains (Blocking Relationships)

```
#4 (Native launcher) ──blocked by──▶ #37 (GHCR publish) ──blocked by──▶ #38 (CI/CD)
                                    └──blocked by──▶ #28 (Launcher contract)
                                    └──feeds into──▶ #39 (Package & sign)

#7 (Resource limits) ──feeds into──▶ #22 (Persisted settings)

#5 (Optional tools) ──feeds into──▶ #26 (Delivery architecture)
                                    └──feeds into──▶ #40 (Pre-pull)

#6 (Job progress) ──feeds into──▶ #24 (Progress model)

#18 (User DAGs) ──feeds into──▶ #20 (DAG persistence model)

#41 (Compression epic) ──feeds into──▶ #42 (Policy decision)
                                    └──feeds into──▶ #43 (pigz tool)
                                    └──feeds into──▶ #44 (FASTQ compression)
                                    └──feeds into──▶ #45 (Apply to uploads/outputs)
```

---

### Summary

- **27 open issues** total (up from 19 in the original backlog sync plan)
- **8 new issues** added since the initial sync: #28–#46 (launcher contract, MCP server, Hermes agent, volume migration, compression epic, pigz, FASTQ compression, compression policy, CI/CD, packaging, amd64 runner, Q&A chat, AI narratives, AI explanations, ARIA fixes)
- **1 confirmed bug** (#10 — disappearing QC report directories) with high priority
- **9 epics** that need to be decomposed into executable child issues
- **6 issues** with `status: specification document` or `status: implementation plan` — still in design phase
- **3 issues** with `status: ready` — spec complete and ready for implementation