import type { SVGProps } from "react";

function Icon({ children, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" fill="none" aria-hidden="true" {...props}>
      {children}
    </svg>
  );
}

export const ChevronDownIcon = (props: SVGProps<SVGSVGElement>) => (
  <Icon {...props}>
    <path d="m7 9.5 5 5 5-5" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
  </Icon>
);

export const SearchIcon = (props: SVGProps<SVGSVGElement>) => (
  <Icon {...props}>
    <circle cx="10.7" cy="10.7" r="6.5" stroke="currentColor" strokeWidth="1.7" />
    <path d="m15.7 15.7 4 4" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
  </Icon>
);

export const TrashIcon = (props: SVGProps<SVGSVGElement>) => (
  <Icon {...props}>
    <path
      d="M5 7h14M9 7V4h6v3m2 0-1 13H8L7 7"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
  </Icon>
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
