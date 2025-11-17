import * as React from "react"
import { cn } from "@/lib/utils"

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "default" | "outline" | "ghost" | "destructive"
  size?: "default" | "sm" | "lg" | "icon"
}

const buttonVariants = (props: { variant?: ButtonProps['variant'], size?: ButtonProps['size'] } = {}) => {
  const { variant = "default", size = "default" } = props
  return cn(
    "inline-flex items-center justify-center rounded-md font-medium transition-colors",
    "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2",
    "disabled:pointer-events-none disabled:opacity-50",
    {
      "bg-blue-600 text-white hover:bg-blue-700": variant === "default",
      "border border-gray-300 bg-white hover:bg-gray-50": variant === "outline",
      "hover:bg-gray-100": variant === "ghost",
      "bg-red-600 text-white hover:bg-red-700": variant === "destructive",
      "h-10 px-4 py-2": size === "default",
      "h-9 px-3 text-sm": size === "sm",
      "h-11 px-8": size === "lg",
      "h-10 w-10": size === "icon",
    }
  )
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "default", size = "default", ...props }, ref) => {
    return (
      <button
        className={cn(buttonVariants({ variant, size }), className)}
        ref={ref}
        {...props}
      />
    )
  }
)
Button.displayName = "Button"

export { Button, buttonVariants }

