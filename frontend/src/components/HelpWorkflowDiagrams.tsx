import { Link } from "react-router-dom";
import diagram1 from "../assets/workflow-diagrams/01-assembly-loops.svg?raw";
import diagram2 from "../assets/workflow-diagrams/02-orchestration.svg?raw";
import diagram3 from "../assets/workflow-diagrams/03-annotation-fanin.svg?raw";
import diagram4 from "../assets/workflow-diagrams/04-analysis-lattice.svg?raw";
import diagram5 from "../assets/workflow-diagrams/05-downstream-trades.svg?raw";
import dl1Png from "../assets/workflow-diagrams/downloads/01-assembly-loops.png";
import dl1Svg from "../assets/workflow-diagrams/downloads/01-assembly-loops.svg";
import dl2Png from "../assets/workflow-diagrams/downloads/02-orchestration.png";
import dl2Svg from "../assets/workflow-diagrams/downloads/02-orchestration.svg";
import dl3Png from "../assets/workflow-diagrams/downloads/03-annotation-fanin.png";
import dl3Svg from "../assets/workflow-diagrams/downloads/03-annotation-fanin.svg";
import dl4Png from "../assets/workflow-diagrams/downloads/04-analysis-lattice.png";
import dl4Svg from "../assets/workflow-diagrams/downloads/04-analysis-lattice.svg";
import dl5Png from "../assets/workflow-diagrams/downloads/05-downstream-trades.png";
import dl5Svg from "../assets/workflow-diagrams/downloads/05-downstream-trades.svg";

/**
 * Five diagrams of a genome assembly + downstream-analysis project, imported
 * from "From Reads to Biology -- A Field Guide to Genome Assembly and
 * Downstream Analysis" (see HelpGenomeAnalysisReview). Each SVG is
 * self-contained (its own <style>, its own light/dark palette keyed off
 * prefers-color-scheme) and unrelated to BioFlow's own theme variables, so it
 * renders via dangerouslySetInnerHTML rather than being redrawn against
 * BioFlow's palette.
 */
const DIAGRAMS: {
  id: string;
  n: string;
  title: string;
  sub: string;
  desc: string;
  svg: string;
  pngUrl: string;
  svgUrl: string;
  fileBase: string;
}[] = [
  {
    id: "d01-assembly-loops",
    n: "Diagram 1",
    title: "The assembly pipeline",
    sub: "Four loops chained by gates — not a staircase",
    desc: "Phases 0–10 with the software at each step. The back-edges carry the meaning: quality comes from re-entering a phase, not from advancing to the next one. Loops 1–3 buy contiguity by spending certainty; Loop 4 is where certainty is measured.",
    svg: diagram1,
    pngUrl: dl1Png,
    svgUrl: dl1Svg,
    fileBase: "01-assembly-loops",
  },
  {
    id: "d02-orchestration",
    n: "Diagram 2",
    title: "Project orchestration",
    sub: "What runs in parallel, and where the tracks collide",
    desc: "Three data types enter independently; two stall waiting on a third. The critical path is A → B → C → annotation, and the only genuinely parallel work is the dashed byproducts that projects routinely skip.",
    svg: diagram2,
    pngUrl: dl2Png,
    svgUrl: dl2Svg,
    fileBase: "02-orchestration",
  },
  {
    id: "d03-annotation-fanin",
    n: "Diagram 3",
    title: "Annotation",
    sub: "A fan-in of six evidence streams, one combiner, three QC legs",
    desc: "The evidence hierarchy is a ranking, not a menu. Annotation tools differ far less than the evidence you feed them — which is why annotation quality dominates every comparative result downstream.",
    svg: diagram3,
    pngUrl: dl3Png,
    svgUrl: dl3Svg,
    fileBase: "03-annotation-fanin",
  },
  {
    id: "d04-analysis-lattice",
    n: "Diagram 4",
    title: "The analysis ladder",
    sub: "A lattice, not a staircase",
    desc: "Ten rungs and the feedback edges that make the ordering a lie. Rung 2 gates rung 3, rung 9 silently corrupts rung 4, and rung 10 is the only one that can falsify the other nine.",
    svg: diagram4,
    pngUrl: dl4Png,
    svgUrl: dl4Svg,
    fileBase: "04-analysis-lattice",
  },
  {
    id: "d05-downstream-trades",
    n: "Diagram 5",
    title: "Downstream workflows",
    sub: "Concurrent programmes that trade outputs",
    desc: "Four analysis programmes that can all start the day an annotation lands. Run in isolation each produces a list; the red cross-edges are nobody's deliverable and are where the causal claims come from.",
    svg: diagram5,
    pngUrl: dl5Png,
    svgUrl: dl5Svg,
    fileBase: "05-downstream-trades",
  },
];

/** Down-arrow-into-tray glyph; no icon library in this repo, so it's drawn
 *  inline rather than pulling one in for a single icon. */
function DownloadIcon() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M8 1.5v8.5" />
      <path d="M4.5 6.5 8 10l3.5-3.5" />
      <path d="M2 12v1.5A1.5 1.5 0 0 0 3.5 15h9a1.5 1.5 0 0 0 1.5-1.5V12" />
    </svg>
  );
}

export function HelpWorkflowDiagrams() {
  return (
    <div className="help-page workflow-diagrams-page">
      <div className="workflow-diagrams-layout">
        {/* Sticky, not fixed: the five diagrams run long enough that a
            reader who scrolled past #1 needs a way back that doesn't require
            scrolling all the way up, mirroring the source document's own
            side nav. */}
        <nav className="workflow-diagrams-nav" aria-label="Diagrams">
          <div className="workflow-diagrams-nav-label">Diagrams</div>
          {DIAGRAMS.map((d) => (
            <a key={d.id} href={`#${d.id}`}>
              <b>
                {d.n.replace("Diagram ", "")} &middot; {d.title}
              </b>
              <span>{d.sub}</span>
            </a>
          ))}
        </nav>

        <div className="workflow-diagrams-content">
          <h1>Workflow Diagrams</h1>
          <p className="help-intro">
            Five views of the same genome assembly and downstream-analysis
            project, drawn to show what actually runs at the same time and
            what is secretly waiting on what. Companion to{" "}
            <Link to="/help/genome-analysis-review">
              <em>
                From Reads to Biology &mdash; a field guide to genome assembly
                and downstream analysis
              </em>
            </Link>
            .
          </p>

          <div className="workflow-diagrams-contract">
            <h2>Encoding contract &mdash; identical in all five diagrams</h2>
            <div className="workflow-diagrams-keys">
              <span>
                <i className="wfd-k1" />
                long-read DNA lineage
              </span>
              <span>
                <i className="wfd-k2" />
                Hi-C / proximity lineage
              </span>
              <span>
                <i className="wfd-k3" />
                RNA lineage
              </span>
              <span>
                <i className="wfd-k4" />
                derived / method step
              </span>
              <span>
                <i className="wfd-k5" />
                free byproduct of data already collected
              </span>
              <span>
                <i className="wfd-k6" />
                gate, blocker or failure edge
              </span>
            </div>
            <p className="workflow-diagrams-contract-note">
              Hue always means <em>source data type</em>, never rank or
              importance. Every node is directly labelled, so identity is
              never carried by colour alone. The three track hues were
              validated for colour-vision deficiency across all pairs in both
              light and dark mode; anything past three categories is encoded
              by dash pattern and position instead of a fourth hue.
            </p>
          </div>

          {DIAGRAMS.map((d) => (
            <figure key={d.id} id={d.id} className="workflow-diagrams-figure">
              <figcaption>
                <div className="workflow-diagrams-figure-n">{d.n}</div>
                <h3>{d.title}</h3>
                <p className="workflow-diagrams-figure-sub">{d.sub}</p>
                <p>{d.desc}</p>
              </figcaption>
              <div className="workflow-diagrams-frame">
                <div
                  // eslint-disable-next-line react/no-danger -- static,
                  // locally authored SVG shipped alongside this component;
                  // no user input.
                  dangerouslySetInnerHTML={{ __html: d.svg }}
                />
                <div className="workflow-diagrams-downloads">
                  <a
                    href={d.pngUrl}
                    download={`${d.fileBase}.png`}
                    title={`Download ${d.title} as PNG`}
                  >
                    <DownloadIcon />
                    PNG
                  </a>
                  <a
                    href={d.svgUrl}
                    download={`${d.fileBase}.svg`}
                    title={`Download ${d.title} as SVG`}
                  >
                    <DownloadIcon />
                    SVG
                  </a>
                </div>
              </div>
            </figure>
          ))}
        </div>
      </div>
    </div>
  );
}
