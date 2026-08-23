import { statusLabel, type CompanionStatus } from "@/lib/protocol";

type MascotBubbleProps = {
  status: CompanionStatus;
  avatarUrl?: string | null;
  botName?: string;
  showLabel?: boolean;
};

export function MascotBubble({
  status,
  avatarUrl,
  botName = "LimeBot",
  showLabel = true,
}: MascotBubbleProps) {
  return (
    <div className={`mascot-bubble mascot-${status}`}>
      <div className="mascot-orbit" />
      {avatarUrl ? (
        <img className="mascot-image" src={avatarUrl} alt={`${botName} avatar`} />
      ) : (
        <div className="mascot-image mascot-fallback" aria-label={`${botName} ${status}`}>
          🍋
        </div>
      )}
      {showLabel ? <span className="mascot-label">{statusLabel(status)}</span> : null}
    </div>
  );
}
