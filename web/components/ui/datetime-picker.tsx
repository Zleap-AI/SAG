"use client"

import * as React from "react"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"

interface DateTimePickerProps {
  value?: Date
  onChange?: (date: Date | undefined) => void
  placeholder?: string
  className?: string
}

export function DateTimePicker({
  value,
  onChange,
  placeholder = "选择默认日期时间",
  className,
}: DateTimePickerProps) {
  // 将 Date 转换为 datetime-local 格式: YYYY-MM-DDTHH:mm
  const formatDateTimeLocal = (date: Date | undefined): string => {
    if (!date) return ""
    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, "0")
    const day = String(date.getDate()).padStart(2, "0")
    const hours = String(date.getHours()).padStart(2, "0")
    const minutes = String(date.getMinutes()).padStart(2, "0")
    return `${year}-${month}-${day}T${hours}:${minutes}`
  }

  const [inputValue, setInputValue] = React.useState<string>(
    formatDateTimeLocal(value)
  )

  React.useEffect(() => {
    setInputValue(formatDateTimeLocal(value))
  }, [value])

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newValue = e.target.value
    setInputValue(newValue)

    if (newValue) {
      // 将 datetime-local 格式转换为 Date 对象
      const date = new Date(newValue)
      if (!isNaN(date.getTime())) {
        onChange?.(date)
      }
    } else {
      onChange?.(undefined)
    }
  }

  return (
    <Input
      type="datetime-local"
      value={inputValue}
      onChange={handleChange}
      placeholder={placeholder}
      className={cn(
        "h-10 bg-white",
        "[&::-webkit-calendar-picker-indicator]:cursor-pointer",
        "[&::-webkit-datetime-edit]:px-1",
        "[&::-webkit-datetime-edit-fields-wrapper]:px-1",
        className
      )}
    />
  )
}
