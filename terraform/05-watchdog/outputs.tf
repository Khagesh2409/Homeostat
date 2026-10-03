output "watchdog_instance_id" {
  description = "EC2 Instance ID of the watchdog server"
  value       = aws_instance.watchdog.id
}

output "watchdog_public_ip" {
  description = "Public IP address of the watchdog server"
  value       = aws_instance.watchdog.public_ip
}

output "watchdog_private_ip" {
  description = "Private IP address of the watchdog server"
  value       = aws_instance.watchdog.private_ip
}

output "watchdog_endpoint" {
  description = "Watchdog service HTTP endpoint"
  value       = "http://${aws_instance.watchdog.private_ip}:8000"
}
