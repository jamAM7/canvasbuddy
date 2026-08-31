// Per-subject, per-week note view (word-editor style).
export default function NoteDetailPage({
  params,
}: {
  params: { subjectId: string; weekId: string };
}) {
  return <main>{/* TODO: word editor bound to `notes.content` */}</main>;
}
