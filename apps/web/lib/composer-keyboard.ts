export type ComposerCompositionState = {
  composing: boolean;
  commitGuard: boolean;
};

export type ComposerKeyboardEventLike = {
  key: string;
  nativeEvent: {
    isComposing?: boolean;
    keyCode?: number;
  };
};

export function isComposerCompositionKeyEvent(
  event: ComposerKeyboardEventLike,
  state: ComposerCompositionState,
): boolean {
  return (
    state.composing ||
    Boolean(event.nativeEvent.isComposing) ||
    event.nativeEvent.keyCode === 229 ||
    (state.commitGuard && event.key === "Enter")
  );
}

export function shouldSubmitAfterEnter(valueBefore: string, valueAfter: string): boolean {
  return valueAfter === valueBefore || valueAfter === `${valueBefore}\n`;
}
