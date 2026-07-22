import * as Dialog from "@radix-ui/react-dialog";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";

import { dialogOverlayMotion, dialogSurfaceMotion } from "../app/motion";
import styles from "../styles/app.module.css";

export function MotionDialogPortal({
  open,
  className,
  children,
  onOpenAutoFocus,
  onEscapeKeyDown,
  onPointerDownOutside,
}: {
  open: boolean;
  className?: string;
  children: React.ReactNode;
  onOpenAutoFocus?: React.ComponentProps<typeof Dialog.Content>["onOpenAutoFocus"];
  onEscapeKeyDown?: React.ComponentProps<typeof Dialog.Content>["onEscapeKeyDown"];
  onPointerDownOutside?: React.ComponentProps<typeof Dialog.Content>["onPointerDownOutside"];
}) {
  return (
    <AnimatePresence>
      {open && (
        <Dialog.Portal forceMount>
          <Dialog.Overlay asChild forceMount>
            <m.div className={styles.dialogOverlay} {...dialogOverlayMotion} />
          </Dialog.Overlay>
          <Dialog.Content
            forceMount
            className={styles.dialogPositioner}
            onOpenAutoFocus={onOpenAutoFocus}
            onEscapeKeyDown={onEscapeKeyDown}
            onPointerDownOutside={onPointerDownOutside}
          >
            <m.div className={className} {...dialogSurfaceMotion}>
              {children}
            </m.div>
          </Dialog.Content>
        </Dialog.Portal>
      )}
    </AnimatePresence>
  );
}
