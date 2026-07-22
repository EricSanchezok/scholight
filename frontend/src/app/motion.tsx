import { AnimatePresence, LazyMotion, MotionConfig } from "motion/react";
import * as m from "motion/react-m";
import { Suspense } from "react";
import { Outlet, useLocation } from "react-router-dom";

import { RouteSkeleton } from "../components/EditorialSkeleton";
import { styles } from "../styles/classes";

export const motionEase = [0.22, 1, 0.36, 1] as const;
export const motionDuration = {
  exit: 0.08,
  routeExit: 0.09,
  quick: 0.1,
  feedback: 0.12,
  popover: 0.14,
  reveal: 0.16,
  standard: 0.18,
  slow: 0.28,
  skeletonPulse: 1.6,
  searchActivity: 1.8,
} as const;

const stagger = { step: 0.02, maximum: 0.16 } as const;

function staggerDelay(index: number): number {
  return Math.min(index * stagger.step, stagger.maximum);
}

export function chevronMotion(open: boolean) {
  return {
    animate: { rotate: open ? 180 : 0 },
    transition: { duration: motionDuration.popover },
  } as const;
}

export function ledgerRowMotion(index: number) {
  return {
    initial: { opacity: 0, y: 4 },
    animate: { opacity: 1, y: 0 },
    transition: { duration: motionDuration.reveal, delay: staggerDelay(index) },
  } as const;
}

export function resultRowMotion(index: number) {
  return {
    initial: { opacity: 0, y: 5 },
    animate: { opacity: 1, y: 0 },
    transition: { duration: motionDuration.standard, delay: staggerDelay(index) },
  } as const;
}

export const sectionRevealMotion = {
  initial: { opacity: 0, y: 4 },
  animate: {
    opacity: 1,
    y: 0,
    transition: { duration: motionDuration.reveal },
  },
  exit: { opacity: 0, transition: { duration: motionDuration.exit } },
} as const;

export function metricRevealMotion(index: number) {
  return {
    initial: { opacity: 0, y: 3 },
    animate: { opacity: 1, y: 0 },
    transition: { duration: motionDuration.reveal, delay: staggerDelay(index) },
  } as const;
}

export const resultsRevealMotion = {
  initial: { opacity: 0 },
  animate: { opacity: 1, transition: { duration: motionDuration.popover } },
  exit: { opacity: 0, transition: { duration: motionDuration.exit } },
} as const;

export const buttonLabelMotion = {
  initial: { opacity: 0, y: 3 },
  animate: {
    opacity: 1,
    y: 0,
    transition: { duration: motionDuration.feedback },
  },
  exit: {
    opacity: 0,
    y: -2,
    transition: { duration: motionDuration.exit },
  },
} as const;

export const mobileMenuMotion = {
  initial: { height: 0, opacity: 0 },
  animate: {
    height: "auto",
    opacity: 1,
    transition: { duration: motionDuration.standard },
  },
  exit: {
    height: 0,
    opacity: 0,
    transition: { duration: motionDuration.quick },
  },
} as const;

export function skeletonPulseMotion(reduceMotion: boolean | null) {
  return {
    animate: reduceMotion ? { opacity: 0.64 } : { opacity: [0.48, 0.72, 0.48] },
    transition: reduceMotion
      ? { duration: 0 }
      : {
          duration: motionDuration.skeletonPulse,
          repeat: Infinity,
          ease: "easeInOut" as const,
        },
  } as const;
}

export function searchActivityMotion(reduceMotion: boolean | null) {
  return {
    animate: reduceMotion
      ? { opacity: 0.7, scaleX: 0.32 }
      : { opacity: [0.45, 1, 0.45], scaleX: [0.12, 0.82, 0.12] },
    transition: reduceMotion
      ? { duration: 0 }
      : {
          duration: motionDuration.searchActivity,
          repeat: Infinity,
          ease: "easeInOut" as const,
        },
  } as const;
}

export const quotaProgressMotion = {
  initial: { width: 0 },
  transition: { duration: motionDuration.slow },
} as const;

export const dialogOverlayMotion = {
  initial: { opacity: 0 },
  animate: {
    opacity: 1,
    transition: { duration: motionDuration.popover, ease: motionEase },
  },
  exit: {
    opacity: 0,
    transition: { duration: motionDuration.quick, ease: motionEase },
  },
} as const;

export const popoverMotion = {
  initial: { opacity: 0, y: -4, scale: 0.985 },
  animate: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: motionDuration.popover, ease: motionEase },
  },
  exit: {
    opacity: 0,
    y: -3,
    scale: 0.99,
    transition: { duration: motionDuration.routeExit, ease: motionEase },
  },
} as const;

export const dialogSurfaceMotion = {
  initial: { opacity: 0, y: 4, scale: 0.985 },
  animate: {
    opacity: 1,
    y: 0,
    scale: 1,
    transition: { duration: motionDuration.standard, ease: motionEase },
  },
  exit: {
    opacity: 0,
    y: 2,
    scale: 0.99,
    transition: { duration: motionDuration.feedback, ease: motionEase },
  },
} as const;

const loadMotionFeatures = () => import("./motionFeatures").then((module) => module.default);

export function ScholightMotionProvider({ children }: { children: React.ReactNode }) {
  return (
    <LazyMotion features={loadMotionFeatures} strict>
      <MotionConfig reducedMotion="user" transition={{ ease: motionEase }}>
        {children}
      </MotionConfig>
    </LazyMotion>
  );
}

export function AnimatedOutlet() {
  const location = useLocation();
  return (
    <div className={styles.routeStage}>
      <AnimatePresence initial={false} mode="wait">
        <m.div
          className={styles.routeScene}
          key={location.pathname}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0, transition: { duration: motionDuration.standard } }}
          exit={{
            opacity: 0,
            y: -2,
            transition: { duration: motionDuration.routeExit },
          }}
        >
          <Suspense fallback={<RouteSkeleton pathname={location.pathname} />}>
            <Outlet />
          </Suspense>
        </m.div>
      </AnimatePresence>
    </div>
  );
}
