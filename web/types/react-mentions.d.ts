declare module 'react-mentions' {
  import { Component, ReactNode, CSSProperties } from 'react'

  export interface MentionItem {
    id: string
    display: string
  }

  export interface MentionStyle {
    control?: CSSProperties
    highlighter?: CSSProperties
    input?: CSSProperties
    '&singleLine'?: {
      control?: CSSProperties
      highlighter?: CSSProperties
      input?: CSSProperties
      display?: string
      width?: string
      minWidth?: string
    }
    '&multiLine'?: {
      control?: CSSProperties
      highlighter?: CSSProperties
      input?: CSSProperties
    }
    suggestions?: {
      list?: CSSProperties & { zIndex?: number; position?: string }
      item?: CSSProperties & {
        '&focused'?: CSSProperties
      }
    }
  }

  export interface MentionsInputProps {
    value: string
    onChange: (event: { target: { value: string } }) => void
    onKeyDown?: (event: React.KeyboardEvent) => void
    placeholder?: string
    style?: MentionStyle
    singleLine?: boolean
    children?: ReactNode
    className?: string
    disabled?: boolean
    suggestionsPortalHost?: HTMLElement
    allowSuggestionsAboveCursor?: boolean
  }

  export interface MentionProps {
    trigger: string
    data: MentionItem[] | ((search: string) => MentionItem[])
    renderSuggestion?: (
      suggestion: MentionItem,
      search: string,
      highlightedDisplay: ReactNode,
      index: number,
      focused: boolean
    ) => ReactNode
    markup?: string
    displayTransform?: (id: string, display: string) => string
    regex?: RegExp
    onAdd?: (id: string, display: string) => void
    appendSpaceOnAdd?: boolean
    style?: CSSProperties
    className?: string
  }

  export class MentionsInput extends Component<MentionsInputProps> {}
  export class Mention extends Component<MentionProps> {}
}

