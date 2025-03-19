import { Link } from "react-router-dom";
import { User } from "../types/types";
import React from "react";

export function UserList({ users }: { users: User[] }) {
  return (
    <ul>
      {users.map((user) => (
        <li key={user.id} className="mb-2">
          {/* Wrap the entire li content in a Link */}
          <Link to={`/user/${user.id}`} className="block text-blue-500 hover:bg-gray-200 p-4 rounded-lg transition-all">
            <div className="flex items-center">
              <p className="text-lg font-semibold">{user.name}</p>
            </div>
          </Link>
        </li>
      ))}
    </ul>
  );
}
