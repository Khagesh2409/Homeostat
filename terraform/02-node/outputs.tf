output "node_public_ip" {
  description = "The Elastic IP attached to the node"
  value       = aws_eip.node_eip.public_ip
}

output "node_instance_id" {
  description = "The EC2 instance ID"
  value       = aws_instance.node.id
}
