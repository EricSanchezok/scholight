import * as Dialog from "@radix-ui/react-dialog";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";

import { dialogSurfaceMotion, motionEase } from "../app/motion";
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
            <m.div
              className={styles.dialogOverlay}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1, transition: { duration: 0.14, ease: motionEase } }}
              exit={{ opacity: 0, transition: { duration: 0.1, ease: motionEase } }}
            />
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
