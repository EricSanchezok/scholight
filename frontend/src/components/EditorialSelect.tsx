import * as Select from "@radix-ui/react-select";

import styles from "../styles/app.module.css";
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
  return (
    <Select.Root value={value} onValueChange={(next) => onValueChange(next as T)}>
      <Select.Trigger
        className={`${styles.selectTrigger} ${variant === "strength" ? styles.strengthSelectTrigger : styles.fieldSelectTrigger}`}
        aria-label={label}
      >
        <Select.Value />
        <Select.Icon className={styles.selectChevron}>
          <ChevronDownIcon />
        </Select.Icon>
      </Select.Trigger>
      <Select.Portal>
        <Select.Content
          className={styles.selectContent}
          position="popper"
          sideOffset={5}
          align="start"
        >
          <Select.Viewport className={styles.selectViewport}>
            {options.map((option) => (
              <Select.Item className={styles.selectItem} value={option.value} key={option.value}>
                <Select.ItemIndicator className={styles.selectIndicator}>✓</Select.ItemIndicator>
                <Select.ItemText>{option.label}</Select.ItemText>
              </Select.Item>
            ))}
          </Select.Viewport>
        </Select.Content>
      </Select.Portal>
    </Select.Root>
  );
}
