// Import all icon PNGs
import fasta from "./01-fasta.png";
import fa from "./02-fa.png";
import fn from "./03-fn.png";
import faa from "./04-faa.png";
import fastq from "./05-fastq.png";
import read from "./06-read.png";
import sam from "./07-sam.png";
import bam from "./08-bam.png";
import cram from "./09-cram.png";
import bed from "./10-bed.png";
import gff from "./11-gff.png";
import gtf from "./12-gtf.png";
import vcf from "./13-vcf.png";
import wig from "./14-wig.png";
import ref from "./15-ref.png";

const FORMAT_ICONS: Record<string, string> = {
  fasta,
  fa,
  fn,
  faa,
  fastq,
  sam,
  bam,
  cram,
  bed,
  gff,
  genbank: gff,
  gtf,
  vcf,
  wig,
};

/**
 * Get the icon path for a given format kind.
 * Returns the specific icon if available, otherwise returns the generic read icon.
 * Reference files always get the reference icon regardless of format.
 */
export function getFileIcon(
  formatKind: string,
  role: string | null | undefined
): string {
  // References always get the reference icon
  if (role === "reference") {
    return ref;
  }

  // Try to get format-specific icon, fallback to generic read icon
  const icon = FORMAT_ICONS[formatKind.toLowerCase()];
  return icon ?? read;
}
