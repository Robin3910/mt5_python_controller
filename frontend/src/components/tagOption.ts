export type TagTone = 'buy' | 'sell' | 'close' | 'on' | 'off'

export interface TagOption<T extends string | number | boolean = string | number | boolean> {
  value: T
  label: string
  hint?: string
  tone?: TagTone
  disabled?: boolean
}
