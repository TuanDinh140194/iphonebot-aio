function StatusDot({ running }) {
  return (
    <span
      style={{
        display: "inline-block",
        width: "10px",
        height: "10px",
        borderRadius: "50%",
        backgroundColor: running ? "#4ade80" : "#f87171",
        marginRight: "8px",
        animation: running ? "pulse-green 1.5s ease-in-out infinite" : "none",
      }}
    />
  );
}

export default StatusDot;
