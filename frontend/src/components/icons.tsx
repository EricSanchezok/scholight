import type { ImgHTMLAttributes, SVGProps } from "react";

import chevronDown from "../assets/icons/chevron-down.svg";
import search from "../assets/icons/search.svg";
import trashDanger from "../assets/icons/trash-danger.svg";
import trashMuted from "../assets/icons/trash-muted.svg";

function Icon({ children, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" aria-hidden="true" {...props}>
      {children}
    </svg>
  );
}

type AssetIconProps = Omit<ImgHTMLAttributes<HTMLImageElement>, "src" | "alt">;

export const ChevronDownIcon = (props: AssetIconProps) => (
  <img src={chevronDown} alt="" aria-hidden="true" width="12" height="12" {...props} />
);

export const SearchIcon = (props: AssetIconProps) => (
  <img src={search} alt="" aria-hidden="true" width="15" height="15" {...props} />
);

export const TrashIcon = (props: AssetIconProps) => (
  <img src={trashDanger} alt="" aria-hidden="true" width="14" height="14" {...props} />
);

export const DeleteSearchIcon = (props: AssetIconProps) => (
  <img src={trashMuted} alt="" aria-hidden="true" width="15" height="15" {...props} />
);

export const MenuIcon = (props: SVGProps<SVGSVGElement>) => (
  <Icon {...props}>
    <path
      d="M4 7h16M4 12h16M4 17h16"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
    />
  </Icon>
);

export const CloseIcon = (props: SVGProps<SVGSVGElement>) => (
  <Icon {...props}>
    <path d="m6 6 12 12M18 6 6 18" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
  </Icon>
);

export const RefreshIcon = (props: SVGProps<SVGSVGElement>) => (
  <Icon {...props}>
    <path
      d="M19 8.5A7.5 7.5 0 1 0 19.1 15M19 4.5v4h-4"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </Icon>
);
