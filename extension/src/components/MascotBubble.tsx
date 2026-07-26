import { statusLabel, type CompanionStatus } from "@/lib/protocol";
import { PetSprite } from "@/components/PetSprite";

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
        <PetSprite status={status} label={`${botName} ${status}`} />
      )}
      {showLabel ? <span className="mascot-label">{statusLabel(status)}</span> : null}
    </div>
  );
}
