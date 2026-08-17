// --- Shares ---

/** Just enough to name one side of a share in a list row -- never the
 *  profile's email, details, or password status. */
export interface ShareParty {
  owner: string;
  username: string;
  emoji: string;
  colour: string;
}

export type ShareState = "offered" | "accepted" | "declined" | "withdrawn";

export interface Share {
  id: string;
  from_owner: string;
  to_owner: string;
  /** Resolved server-side. Never join `from_owner`/`to_owner` against
   *  `/profiles` on the client -- the adopted profile's owner string is the
   *  literal "local", which matches no profile id, so that join silently
   *  renders a blank sender for exactly the profile most likely to be
   *  sharing. */
  from_profile: ShareParty;
  to_profile: ShareParty;
  source_object_id: string;
  name: string;
  size: number;
  state: ShareState;
  accepted_object_id: string | null;
  message: string | null;
  created_at: string;
  updated_at: string;
}
