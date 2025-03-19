import { BrowserRouter as Router, Routes, Route, Link } from "react-router-dom";
import { useFetchUsers } from "./hooks/useFetchUsers.ts";
import { UserList } from "./components/UserList.tsx";
import { UserDetails } from "./components/UserDetails.tsx";
import { ZdjeciaFirmy } from "./components/ZdjeciaFirmy.tsx";

import React from 'react'; // Add this import
import './App.css';
export default function App() {
  return (
    <Router>
      <div className="p-4">
      <nav className="mb-4 flex gap-4"><h1>Firma Kasztanki</h1></nav>

        <nav className="mb-4 flex gap-4">
          <Link to="/home" className="text-blue-500">Home</Link>
          <Link to="/ZdjeciaFirmy" className="text-blue-500">zdiecia</Link>
        </nav>
        <Routes>
          <Route path="/home" element={<Home />} />
          <Route path="/ZdjeciaFirmy" element={<ZdjeciaFirmy /> } />
          <Route path="/user/:id" element={<UserDetails />} />
        </Routes>
      </div>
    </Router>
  );
}

function Home() {
  const { users, loading, error } = useFetchUsers();

  if (loading) return <p>Loading...</p>;
  if (error) return <p>Error loading users.</p>;

  return <UserList users={users} />;
}
