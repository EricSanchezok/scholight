import * as Dialog from "@radix-ui/react-dialog";

import { styles } from "../../styles/classes";
import type { Messages } from "../../i18n/en";

export function InstallInstructionsDialog({
  kind,
  messages,
  open,
  onOpenChange,
}: {
  kind: "android" | "ios" | "in-app";
  messages: Messages["installExperience"];
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const content = messages[kind];
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className={styles.dialogOverlay} />
        <Dialog.Content className={styles.installDialog} aria-describedby="install-description">
          <Dialog.Title className={styles.installDialogTitle}>{content.title}</Dialog.Title>
          <Dialog.Description id="install-description" className={styles.installDialogDescription}>
            {content.description}
          </Dialog.Description>
          <ol className={styles.installSteps}>
            {content.steps.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
          <div className={styles.dialogActions}>
            <Dialog.Close asChild>
              <button className={styles.secondaryButton} type="button">
                {messages.close}
              </button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
