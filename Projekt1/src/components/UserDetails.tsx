import { useParams } from "react-router-dom";
import { useFetchUsers } from "../hooks/useFetchUsers.ts";
import React from "react";


export function UserDetails() {
  const { id } = useParams<{ id: string }>();
  const { users } = useFetchUsers();
  const user = users.find((u) => u.id === Number(id));

  if (!user) return <p>User not found</p>;

  return (
    <div>
      <h2 className="text-xl font-bold">{user.name}</h2>
      <p>Email: {user.email}</p>
      <p>Phone: {user.phone}
        
      </p>
    </div>
  );
}
