
import React from "react";
import { useFetchUsers } from "../hooks/useFetchUsers";
import { UserList } from "../components/UserList";

export function Home() {
  
  const { users, loading, error } = useFetchUsers();

  if (loading) return <p>Loading...</p>;
  
  if (error) {
    console.error(error); 
    return <p className="text-red-500">{error}</p>; 
  }

  return( 
 
  <UserList users={users} />);
  

  
  
}

    
  

