import * as Select from "@radix-ui/react-select";
import { AnimatePresence } from "motion/react";
import * as m from "motion/react-m";
import { useState } from "react";

import { chevronMotion, popoverMotion } from "../app/motion";
import { styles } from "../styles/classes";
import { ChevronDownIcon } from "./icons";

export interface EditorialSelectOption<T extends string> {
  value: T;
  label: string;
}

export function EditorialSelect<T extends string>({
  label,
  value,
  options,
  onValueChange,
  variant = "field",
}: {
  label: string;
  value: T;
  options: readonly EditorialSelectOption<T>[];
  onValueChange: (value: T) => void;
  variant?: "strength" | "field";
}) {
  const [open, setOpen] = useState(false);
  return (
    <Select.Root
      open={open}
      onOpenChange={setOpen}
      value={value}
      onValueChange={(next) => onValueChange(next as T)}
    >
      <Select.Trigger
        className={`${styles.selectTrigger} ${variant === "strength" ? styles.strengthSelectTrigger : styles.fieldSelectTrigger}`}
        aria-label={label}
      >
        <Select.Value>{options.find((option) => option.value === value)?.label}</Select.Value>
        <Select.Icon asChild>
          <m.span className={styles.selectChevron} {...chevronMotion(open)}>
            <ChevronDownIcon />
          </m.span>
        </Select.Icon>
      </Select.Trigger>
      <AnimatePresence>
        {open && (
          <Select.Portal forceMount>
            <Select.Content asChild forceMount position="popper" sideOffset={5} align="start">
              <m.div className={styles.selectContent} {...popoverMotion}>
                <Select.Viewport className={styles.selectViewport}>
                  {options.map((option) => (
                    <Select.Item
                      className={styles.selectItem}
                      value={option.value}
                      key={option.value}
                    >
                      <Select.ItemIndicator className={styles.selectIndicator}>
                        ✓
                      </Select.ItemIndicator>
                      <Select.ItemText>{option.label}</Select.ItemText>
                    </Select.Item>
                  ))}
                </Select.Viewport>
              </m.div>
            </Select.Content>
          </Select.Portal>
        )}
      </AnimatePresence>
    </Select.Root>
  );
}
